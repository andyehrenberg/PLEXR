import logging
import os
from collections import deque
from typing import List

import numpy as np
import torch

import wandb
from level_replay import utils
from level_replay.algo.buffer import make_buffer
from level_replay.algo.policy import DDQN
from level_replay.dqn_args import parser
from level_replay.envs import make_lr_venv
from level_replay.utils import ppo_normalise_reward

os.environ["OMP_NUM_THREADS"] = "1"

last_checkpoint_time = None


def train(args, seeds):
    global last_checkpoint_time
    args.cuda = not args.no_cuda and torch.cuda.is_available()
    args.device = torch.device("cuda:0" if args.cuda else "cpu")
    if "cuda" in args.device.type:
        print("Using CUDA\n")
    args.optimizer_parameters = {"lr": args.learning_rate, "eps": args.adam_eps}
    args.seeds = seeds

    torch.set_num_threads(1)

    utils.seed(args.seed)

    wandb.init(
        settings=wandb.Settings(start_method="fork"),
        project="off-policy-procgen",
        entity="ucl-dark",
        config=vars(args),
        tags=["ddqn", "procgen"] + (args.wandb_tags.split(",") if args.wandb_tags else []),
        group=args.wandb_group,
    )

    num_levels = 1
    level_sampler_args = dict(
        num_actors=args.num_processes,
        strategy=args.level_replay_strategy,
    )
    envs, level_sampler = make_lr_venv(
        num_envs=args.num_processes,
        env_name=args.env_name,
        seeds=seeds,
        device=args.device,
        num_levels=num_levels,
        start_level=args.start_level,
        no_ret_normalization=args.no_ret_normalization,
        distribution_mode=args.distribution_mode,
        paint_vel_info=args.paint_vel_info,
        use_sequential_levels=args.use_sequential_levels,
        level_sampler_args=level_sampler_args,
    )

    replay_buffer = make_buffer(args)

    agent = DDQN(args)

    level_seeds = torch.zeros(args.num_processes)
    if level_sampler:
        state, level_seeds = envs.reset()
    else:
        state = envs.reset()
    level_seeds = level_seeds.unsqueeze(-1)

    episode_reward = 0

    state_deque: List[deque] = [deque(maxlen=args.multi_step) for _ in range(args.num_processes)]
    reward_deque: List[deque] = [deque(maxlen=args.multi_step) for _ in range(args.num_processes)]
    action_deque: List[deque] = [deque(maxlen=args.multi_step) for _ in range(args.num_processes)]

    num_steps = int(args.T_max // args.num_processes)

    value = [torch.tensor(0) for _ in range(args.num_processes)]
    recent_returns = {seed: 0 for seed in seeds}
    s0_value_estimates = {seed: 0 for seed in seeds}

    loss, grad_magnitude = None, None

    epsilon_start = 1.0
    epsilon_final = args.end_eps
    epsilon_decay = args.eps_decay_period

    def epsilon(t):
        return epsilon_final + (epsilon_start - epsilon_final) * np.exp(
            -1.0 * (t - args.start_timesteps) / epsilon_decay
        )

    for t in range(num_steps):
        if t < args.start_timesteps:
            action = (
                torch.LongTensor([envs.action_space.sample() for _ in range(args.num_processes)])
                .reshape(-1, 1)
                .to(args.device)
            )
        else:
            cur_epsilon = epsilon(t)
            action, value = agent.select_action(state)
            for i in range(args.num_processes):
                if np.random.uniform() < cur_epsilon:
                    action[i] = torch.LongTensor([envs.action_space.sample()]).to(args.device)
            wandb.log({"Current Epsilon": cur_epsilon}, step=t * args.num_processes)

        # Perform action and log results
        next_state, reward, done, infos = envs.step(action)

        for i, info in enumerate(infos):
            if "bad_transition" in info.keys():
                print("Bad transition")
            if level_sampler:
                level_seed = info["level_seed"]
                if level_seeds[i][0] != level_seed:
                    level_seeds[i][0] = level_seed
                    new_episode(s0_value_estimates, value, level_seed, i, step=t * args.num_processes)
            state_deque[i].append(state[i])
            reward_deque[i].append(reward[i])
            action_deque[i].append(action[i])
            if len(state_deque[i]) == args.multi_step or done[i]:
                n_reward = multi_step_reward(reward_deque[i], args.gamma)
                n_state = state_deque[i][0]
                n_action = action_deque[i][0]
                replay_buffer.add(
                    n_state, n_action, next_state[i], n_reward, np.uint8(done[i]), level_seeds[i]
                )
                if done[i]:
                    for j in range(1, args.multi_step):
                        n_reward = multi_step_reward(
                            [reward_deque[i][k] for k in range(j, args.multi_step)], args.gamma
                        )
                        n_state = state_deque[i][j]
                        n_action = action_deque[i][j]
                        replay_buffer.add(
                            n_state, n_action, next_state[i], n_reward, np.uint8(done[i]), level_seeds[i]
                        )
            if "episode" in info.keys():
                episode_reward = info["episode"]["r"]
                wandb.log(
                    {
                        "Train Episode Returns": episode_reward,
                        "Train Episode Returns (normalised)": ppo_normalise_reward(
                            episode_reward, args.env_name
                        ),
                    },
                    step=t * args.num_processes,
                )
                state_deque[i].clear()
                reward_deque[i].clear()
                action_deque[i].clear()
                plot_level_returns(
                    recent_returns, level_seeds, episode_reward, i, step=t * args.num_processes
                )

        state = next_state

        # Train agent after collecting sufficient data
        if (t + 1) % args.train_freq == 0 and t >= args.start_timesteps:
            for _ in range(args.num_updates):
                loss, grad_magnitude = agent.train(replay_buffer)
                wandb.log(
                    {"Value Loss": loss, "Gradient magnitude": grad_magnitude}, step=t * args.num_processes
                )

        if t == num_steps - 1:
            count_data = [
                [seed, count] for (seed, count) in zip(agent.seed_weights.keys(), agent.seed_weights.values())
            ]
            total_weight = sum([i[1] for i in count_data])
            count_data = [[i[0], i[1] / total_weight] for i in count_data]
            table = wandb.Table(data=count_data, columns=["Seed", "Weight"])
            wandb.log(
                {
                    "Seed Sampling Distribution at End of Training": wandb.plot.bar(
                        table, "Seed", "Weight", title="Sampling distribution of levels"
                    )
                }
            )

        if t >= args.start_timesteps and (t + 1) % args.eval_freq == 0:
            mean_test_rewards = np.mean(eval_policy(args, agent, args.num_test_seeds))
            mean_train_rewards = np.mean(
                eval_policy(
                    args,
                    agent,
                    args.num_test_seeds,
                    start_level=0,
                    num_levels=args.num_train_seeds,
                    seeds=seeds,
                )
            )
            wandb.log(
                {
                    "Test Evaluation Returns": mean_test_rewards,
                    "Train Evaluation Returns": mean_train_rewards,
                    "Generalization Gap:": mean_train_rewards - mean_test_rewards,
                    "Test Evaluation Returns (normalised)": ppo_normalise_reward(
                        mean_test_rewards, args.env_name
                    ),
                    "Train Evaluation Returns (normalised)": ppo_normalise_reward(
                        mean_train_rewards, args.env_name
                    ),
                }
            )

    print(f"\nLast update: Evaluating on {args.final_num_test_seeds} test levels...\n  ")
    final_eval_episode_rewards = eval_policy(
        args, agent, args.final_num_test_seeds, record=args.record_final_eval
    )

    mean_final_eval_episode_rewards = np.mean(final_eval_episode_rewards)
    median_final_eval_episide_rewards = np.median(final_eval_episode_rewards)

    print("Mean Final Evaluation Rewards: ", mean_final_eval_episode_rewards)
    print("Median Final Evaluation Rewards: ", median_final_eval_episide_rewards)

    wandb.log(
        {
            "Mean Final Evaluation Rewards": mean_final_eval_episode_rewards,
            "Median Final Evaluation Rewards": median_final_eval_episide_rewards,
            "Mean Final Evaluation Rewards (normalised)": ppo_normalise_reward(
                mean_final_eval_episode_rewards, args.env_name
            ),
            "Median Final Evaluation Rewards (normalised)": ppo_normalise_reward(
                median_final_eval_episide_rewards, args.env_name
            ),
        }
    )

    if args.save_model:
        print(f"Saving model to {args.model_path}")
        if "models" not in os.listdir():
            os.mkdir("models")
        torch.save(
            {
                "model_state_dict": agent.Q.state_dict(),
                "args": vars(args),
            },
            args.model_path,
        )


def generate_seeds(num_seeds, base_seed=0):
    return [base_seed + i for i in range(num_seeds)]


def load_seeds(seed_path):
    seed_path = os.path.expandvars(os.path.expanduser(seed_path))
    seeds = open(seed_path).readlines()
    return [int(s) for s in seeds]


def eval_policy(
    args,
    policy,
    num_episodes,
    num_processes=8,
    deterministic=False,
    start_level=0,
    num_levels=0,
    seeds=None,
    level_sampler=None,
    progressbar=None,
    record=False,
):
    if level_sampler:
        start_level = level_sampler.seed_range()[0]
        num_levels = 1

    eval_envs, level_sampler = make_lr_venv(
        num_envs=num_processes,
        env_name=args.env_name,
        seeds=seeds,
        device=args.device,
        num_levels=num_levels,
        start_level=start_level,
        no_ret_normalization=args.no_ret_normalization,
        distribution_mode=args.distribution_mode,
        paint_vel_info=args.paint_vel_info,
        level_sampler=level_sampler,
        record_runs=record,
    )

    eval_episode_rewards: List[float] = []
    if level_sampler:
        state, _ = eval_envs.reset()
    else:
        state = eval_envs.reset()
    while len(eval_episode_rewards) < num_episodes:
        if np.random.uniform() < args.eval_eps:
            action = (
                torch.LongTensor([eval_envs.action_space.sample() for _ in range(num_processes)])
                .reshape(-1, 1)
                .to(args.device)
            )
        else:
            with torch.no_grad():
                action, _ = policy.select_action(state, eval=True)
        state, _, done, infos = eval_envs.step(action)
        for info in infos:
            if "episode" in info.keys():
                eval_episode_rewards.append(info["episode"]["r"])
                if progressbar:
                    progressbar.update(1)

    if record:
        for video in eval_envs.get_videos():
            wandb.log({"evaluation_behaviour": video})

    eval_envs.close()
    if progressbar:
        progressbar.close()

    avg_reward = sum(eval_episode_rewards) / len(eval_episode_rewards)

    print("---------------------------------------")
    print(f"Evaluation over {num_episodes} episodes: {avg_reward}")
    print("---------------------------------------")
    return eval_episode_rewards


def multi_step_reward(rewards, gamma):
    ret = 0.0
    for idx, reward in enumerate(rewards):
        ret += reward * (gamma ** idx)
    return ret


def new_episode(s0_value_estimates, value, level_seed, i, step):
    s0_value_estimates[level_seed] = value[i].item()
    wandb.log(
        {f"Start State Value Estimate for Level {level_seed}": s0_value_estimates[level_seed]}, step=step
    )


def plot_level_returns(recent_returns, level_seeds, episode_reward, i, step):
    seed = level_seeds[i][0].item()
    recent_returns[seed] = episode_reward
    wandb.log({f"Empirical Return for Level {seed}": recent_returns[seed]}, step=step)


if __name__ == "__main__":
    args = parser.parse_args()
    print(args)

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)
    else:
        logging.disable(logging.CRITICAL)

    if args.seed_path:
        train_seeds = load_seeds(args.seed_path)
    else:
        train_seeds = generate_seeds(args.num_train_seeds)

    train(args, train_seeds)
