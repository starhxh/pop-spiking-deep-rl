from copy import deepcopy
import numpy as np
import torch
import torch.nn as nn
from torch.optim import Adam
from torch.utils.tensorboard import SummaryWriter
import gym
import pickle
import os
import sys

sys.path.append('../../../')
from corl_code.popsan_drl.popsan_td3.replay_buffer_norm import ReplayBuffer
from corl_code.popsan_drl.popsan_td3.popsan import PopSpikeActor
from corl_code.popsan_drl.popsan_td3.core_cuda import MLPQFunction


class SpikeActorDeepCritic(nn.Module):

    def __init__(self, observation_space, action_space,
                 encoder_pop_dim, decoder_pop_dim, mean_range, std, spike_ts, device, use_poisson,
                 hidden_sizes=(256, 256), activation=nn.ReLU):
        """
        :param observation_space: observation space from gym
        :param action_space: action space from gym
        :param encoder_pop_dim: encoder population dimension
        :param decoder_pop_dim: decoder population dimension
        :param mean_range: mean range for encoder
        :param std: std for encoder
        :param spike_ts: spike timesteps
        :param device: device
        :param use_poisson: if true use Poisson spikes for encoder
        :param hidden_sizes: list of hidden layer sizes
        :param activation: activation function for critic network
        """
        super().__init__()
        obs_dim = observation_space.shape[0]
        act_dim = action_space.shape[0]
        act_limit = action_space.high[0]
        # build policy and value functions
        self.popsan = PopSpikeActor(obs_dim, act_dim, encoder_pop_dim, decoder_pop_dim, hidden_sizes,
                                    mean_range, std, spike_ts, act_limit, device, use_poisson)
        self.q = MLPQFunction(obs_dim, act_dim, hidden_sizes, activation)

    def act(self, obs, batch_size):
        with torch.no_grad():
            return self.popsan(obs, batch_size).to('cpu').numpy()


def spike_ddpg(env_fn, actor_critic=SpikeActorDeepCritic, ac_kwargs=dict(), seed=0,
               steps_per_epoch=10000, epochs=100, replay_size=int(1e6), gamma=0.99,
               polyak=0.995, popsan_lr=1e-4, q_lr=1e-3, batch_size=100, start_steps=10000,
               update_after=1000, update_every=50, act_noise=0.1, num_test_episodes=10,
               max_ep_len=1000, save_freq=5, norm_clip_limit=3, norm_update=50, tb_comment='', model_idx=0,
               use_cuda=True):
    """
    Spike Deep Deterministic Policy Gradient (Spike-DDPG)


    Args:
        env_fn : A function which creates a copy of the environment.
            The environment must satisfy the OpenAI Gym API.

        actor_critic: The constructor method for a PyTorch Module with an ``act`` 
            method, a ``popsan`` module, and a ``q`` module. The ``act`` method and
            ``popsan`` module should accept batches of observations as inputs,
            and ``q`` should accept a batch of observations and a batch of 
            actions as inputs. When called, these should return:

            ===========  ================  ======================================
            Call         Output Shape      Description
            ===========  ================  ======================================
            ``act``      (batch, act_dim)  | Numpy array of actions for each 
                                           | observation.
            ``popsan``   (batch, act_dim)  | Tensor containing actions from policy
                                           | given observations.
            ``q``        (batch,)          | Tensor containing the current estimate
                                           | of Q* for the provided observations
                                           | and actions. (Critical: make sure to
                                           | flatten this!)
            ===========  ================  ======================================

        ac_kwargs (dict): Any kwargs appropriate for the SpikeActorDeepCritic object
            you provided to DDPG.

        seed (int): Seed for random number generators.

        steps_per_epoch (int): Number of steps of interaction (state-action pairs) 
            for the agent and the environment in each epoch.

        epochs (int): Number of epochs to run and train agent.

        replay_size (int): Maximum length of replay buffer.

        gamma (float): Discount factor. (Always between 0 and 1.)

        polyak (float): Interpolation factor in polyak averaging for target 
            networks. Target networks are updated towards main networks 
            according to:

            .. math:: \\theta_{\\text{targ}} \\leftarrow 
                \\rho \\theta_{\\text{targ}} + (1-\\rho) \\theta

            where :math:`\\rho` is polyak. (Always between 0 and 1, usually 
            close to 1.)

        popsan_lr (float): Learning rate for policy.

        q_lr (float): Learning rate for Q-networks.

        batch_size (int): Minibatch size for SGD.

        start_steps (int): Number of steps for uniform-random action selection,
            before running real policy. Helps exploration.

        update_after (int): Number of env interactions to collect before
            starting to do gradient descent updates. Ensures replay buffer
            is full enough for useful updates.

        update_every (int): Number of env interactions that should elapse
            between gradient descent updates. Note: Regardless of how long 
            you wait between updates, the ratio of env steps to gradient steps 
            is locked to 1.

        act_noise (float): Stddev for Gaussian exploration noise added to 
            policy at training time. (At test time, no noise is added.)

        num_test_episodes (int): Number of episodes to test the deterministic
            policy at the end of each epoch.

        max_ep_len (int): Maximum length of trajectory / episode / rollout.

        save_freq (int): How often (in terms of gap between epochs) to save
            the current policy and value function.

        norm_clip_limit (float): Clip limit for normalize observation

        norm_update (int): Number of steps to update running mean and var in memory

        tb_comment (str): Comment for tensorboard writer

        model_idx (int): Index of training model

        use_cuda (bool): If true use cuda for computation
    """
    # Set device
    if use_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device("cpu")

    # Set random seed
    torch.manual_seed(seed)
    np.random.seed(seed)

    env, test_env = env_fn(), env_fn()
    obs_dim = env.observation_space.shape
    act_dim = env.action_space.shape[0]

    # Action limit for clamping: critically, assumes all dimensions share the same bound!
    act_limit = env.action_space.high[0]

    # Create actor-critic module and target networks
    ac = actor_critic(env.observation_space, env.action_space, **ac_kwargs)
    ac_targ = deepcopy(ac)
    ac.to(device)
    ac_targ.to(device)

    # Freeze target networks with respect to optimizers (only update via polyak averaging)
    for p in ac_targ.parameters():
        p.requires_grad = False

    # Experience buffer
    replay_buffer = ReplayBuffer(obs_dim=obs_dim, act_dim=act_dim, size=replay_size,
                                 clip_limit=norm_clip_limit, norm_update_every=norm_update)

    # Set up function for computing Spike-DDPG Q-loss
    def compute_loss_q(data):
        o, a, r, o2, d = data['obs'], data['act'], data['rew'], data['obs2'], data['done']

        q = ac.q(o, a)

        # Bellman backup for Q function
        with torch.no_grad():
            q_popsan_targ = ac_targ.q(o2, ac_targ.popsan(o2, batch_size))
            backup = r + gamma * (1 - d) * q_popsan_targ

        # MSE loss against Bellman backup
        loss_q = ((q - backup) ** 2).mean()

        # Useful info for logging
        loss_info = dict(QVals=q.to('cpu').detach().numpy())

        return loss_q, loss_info

    # Set up function for computing Spike-DDPG popsan loss
    def compute_loss_popsan(data):
        o = data['obs']
        q_popsan = ac.q(o, ac.popsan(o, batch_size))
        return -q_popsan.mean()

    # Set up optimizers for policy and q-function
    popsan_optimizer = Adam(ac.popsan.parameters(), lr=popsan_lr)
    q_optimizer = Adam(ac.q.parameters(), lr=q_lr)

    def update(data):
        # First run one gradient descent step for Q.
        q_optimizer.zero_grad()
        loss_q, loss_info = compute_loss_q(data)
        loss_q.backward()
        q_optimizer.step()

        # Freeze Q-network so you don't waste computational effort 
        # computing gradients for it during the policy learning step.
        for p in ac.q.parameters():
            p.requires_grad = False

        # Next run one gradient descent step for popsan.
        popsan_optimizer.zero_grad()
        loss_popsan = compute_loss_popsan(data)
        loss_popsan.backward()
        popsan_optimizer.step()

        # Unfreeze Q-network so you can optimize it at next DDPG step.
        for p in ac.q.parameters():
            p.requires_grad = True

        # Finally, update target networks by polyak averaging.
        with torch.no_grad():
            for p, p_targ in zip(ac.parameters(), ac_targ.parameters()):
                # NB: We use an in-place operations "mul_", "add_" to update target
                # params, as opposed to "mul" and "add", which would make new tensors.
                p_targ.data.mul_(polyak)
                p_targ.data.add_((1 - polyak) * p.data)

    def get_action(o, noise_scale):
        a = ac.act(torch.as_tensor(o, dtype=torch.float32, device=device), 1)
        a += noise_scale * np.random.randn(act_dim)
        return np.clip(a, -act_limit, act_limit)

    def test_agent():
        ###
        # compuate the return mean test reward
        ###
        test_reward_sum = 0
        for j in range(num_test_episodes):
            o, d, ep_ret, ep_len = test_env.reset(), False, 0, 0
            while not (d or (ep_len == max_ep_len)):
                # Take deterministic actions at test time (noise_scale=0)
                o, r, d, _ = test_env.step(get_action(replay_buffer.normalize_obs(o), 0))
                ep_ret += r
                ep_len += 1
            test_reward_sum += ep_ret
        return test_reward_sum / num_test_episodes

    ###
    # add tensorboard support and save rewards
    # Also create dir for saving parameters
    ###
    writer = SummaryWriter(comment="_" + tb_comment + "_" + str(model_idx))
    save_test_reward = []
    save_test_reward_steps = []
    try:
        os.mkdir("./params")
        print("Directory params Created")
    except FileExistsError:
        print("Directory params already exists")
    model_dir = "./params/spike-ddpg_" + tb_comment
    try:
        os.mkdir(model_dir)
        print("Directory ", model_dir, " Created")
    except FileExistsError:
        print("Directory ", model_dir, " already exists")

    # Prepare for interaction with environment
    total_steps = steps_per_epoch * epochs
    o, ep_ret, ep_len = env.reset(), 0, 0

    # Main loop: collect experience in env and update/log each epoch
    for t in range(total_steps):

        # Until start_steps have elapsed, randomly sample actions
        # from a uniform distribution for better exploration. Afterwards, 
        # use the learned policy (with some noise, via act_noise). 
        if t > start_steps:
            a = get_action(replay_buffer.normalize_obs(o), act_noise)
        else:
            a = env.action_space.sample()

        # Step the env
        o2, r, d, _ = env.step(a)
        ep_ret += r
        ep_len += 1

        # Ignore the "done" signal if it comes from hitting the time
        # horizon (that is, when it's an artificial terminal signal
        # that isn't based on the agent's state)
        d = False if ep_len == max_ep_len else d

        # Store experience to replay buffer
        replay_buffer.store(o, a, r, o2, d)

        # Super critical, easy to overlook step: make sure to update 
        # most recent observation!
        o = o2

        # End of trajectory handling
        if d or (ep_len == max_ep_len):
            writer.add_scalar(tb_comment + '/Train-Reward', ep_ret, t + 1)
            o, ep_ret, ep_len = env.reset(), 0, 0

        # Update handling
        if t >= update_after and t % update_every == 0:
            for _ in range(update_every):
                batch = replay_buffer.sample_batch(device, batch_size)
                update(data=batch)

        # End of epoch handling
        if (t + 1) % steps_per_epoch == 0:
            epoch = (t + 1) // steps_per_epoch

            # Save model
            if (epoch % save_freq == 0) or (epoch == epochs):
                ac.popsan.to('cpu')
                torch.save(ac.popsan.state_dict(),
                           model_dir + '/' + "model" + str(model_idx) + "_e" + str(epoch) + '.pt')
                print("Learned Mean for encoder population: ")
                print(ac.popsan.encoder.mean.data)
                print("Learned STD for encoder population: ")
                print(ac.popsan.encoder.std.data)
                ac.popsan.to(device)
                pickle.dump([replay_buffer.mean, replay_buffer.var],
                            open(model_dir + '/' + "model" + str(model_idx) + "_e" + str(epoch) + '_mean_var.p', "wb+"))
                print("Weights saved in ", model_dir + '/' + "model" + str(model_idx) + "_e" + str(epoch) + '.pt')

            # Test the performance of the deterministic version of the agent.
            test_mean_reward = test_agent()
            save_test_reward.append(test_mean_reward)
            save_test_reward_steps.append(t+1)
            writer.add_scalar(tb_comment + '/Test-Mean-Reward', test_mean_reward, t + 1)
            print("Model: ", model_idx, " Steps: ", t + 1, " Mean Reward: ", test_mean_reward)

    # Save Test Reward List
    pickle.dump([save_test_reward, save_test_reward_steps],
                open(model_dir + '/' + "model" + str(model_idx) + "_test_rewards.p", "wb+"))


if __name__ == '__main__':
    import math
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--env', type=str, default='Ant-v3')
    parser.add_argument('--encoder_pop_dim', type=int, default=10)
    parser.add_argument('--decoder_pop_dim', type=int, default=10)
    parser.add_argument('--encoder_var', type=float, default=0.15)
    parser.add_argument('--start_model_idx', type=int, default=0)
    parser.add_argument('--num_model', type=int, default=10)
    parser.add_argument('--epochs', type=int, default=100)
    args = parser.parse_args()

    START_MODEL = args.start_model_idx
    NUM_MODEL = args.num_model
    USE_POISSON = False
    if args.env == 'Hopper-v3' or args.env == 'Walker2d-v3':
        USE_POISSON = True
    AC_KWARGS = dict(hidden_sizes=[256, 256],
                     encoder_pop_dim=args.encoder_pop_dim,
                     decoder_pop_dim=args.decoder_pop_dim,
                     mean_range=(-3, 3),
                     std=math.sqrt(args.encoder_var),
                     spike_ts=5,
                     device=torch.device('cuda'),
                     use_poisson=USE_POISSON)
    COMMENT = "ddpg-popsan-" + args.env + "-encoder-dim-" + str(AC_KWARGS['encoder_pop_dim']) + \
              "-decoder-dim-" + str(AC_KWARGS['decoder_pop_dim'])
    for num in range(START_MODEL, START_MODEL + NUM_MODEL):
        seed = num * 10
        spike_ddpg(lambda: gym.make(args.env), actor_critic=SpikeActorDeepCritic, ac_kwargs=AC_KWARGS,
                   popsan_lr=1e-4, gamma=0.99, seed=seed, epochs=args.epochs,
                   norm_clip_limit=3.0, tb_comment=COMMENT, model_idx=num)
