# %%
import os
import sys
sys.path.append('../')

import argparse
import numpy as np
import copy
import pandas as pd
import time

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

import gym
import numpy as np
import cv2
import matplotlib.pyplot as plt

from gym.core import ObservationWrapper
from gym.spaces.box import Box
from PIL import Image
from IPython.display import clear_output
from tqdm import trange

from material.atari_util import *
from material.atari_wrapper import *

parser = argparse.ArgumentParser(description='Pytorch A3C PongDeterministic')
parser.add_argument('--lr', type=float, default=0.0001,
                    help='learning rate (default: 0.0001)')
parser.add_argument('--gamma', type=float, default=0.99,
                    help='discount factor for rewards (default: 0.99)')
parser.add_argument('--entropy-coef', type=float, default=0.01,
                    help='entropy term coefficient (default: 0.01)')
parser.add_argument('--value-loss-coef', type=float, default=0.5,
                    help='value loss coefficient (default: 0.5)')
parser.add_argument('--max-grad-norm', type=float, default=50,
                    help='value loss coefficient (default: 50)')
parser.add_argument('--seed', type=int, default=123,
                    help='random seed (default: 123)')
parser.add_argument('--num-processes', type=int, default=4,
                    help='how many training processes to use (default: 4)')
parser.add_argument('--num-steps', type=int, default=20,
                    help='number of forward steps in A3C (default: 20)')
parser.add_argument('--max-episode-length', type=int, default=1000000,
                    help='maximum length of an episode (default: 1000000)')
parser.add_argument('--env-name', default='PongDeterministic-v4',
                    help='environment to train on (default: PongDeterministic-v4)')

device = torch.device('cuda')

"""
# Pong environment preprocess
"""
class PreprocessAtariObs(ObservationWrapper):
    def __init__(self, env):
        """A gym wrapper that crops, scales image into the desired shapes and grayscales it."""
        ObservationWrapper.__init__(self, env)

        self.img_size = (1, 42, 42)
        self.observation_space = Box(0.0, 1.0, self.img_size,dtype=np.float32)

    def _to_gray_scale(self, rgb, channel_weights=[0.7, 0.1, 0.2]):
        dummy = 0
        for idx,channel_weight in enumerate(channel_weights):
            dummy += channel_weight*(rgb[:,:,idx])
        return np.expand_dims(dummy,axis=-1)
    
    def observation(self, img):      
        img = img[34:34+160, :160]
        img = Image.fromarray(np.uint8(img),'RGB')
        img = img.resize((42,42))
        img = np.array(img)
        img = self._to_gray_scale(img)/255.
        return np.array(img,dtype=np.float32).transpose((2,0,1))

class NormalizedEnv(gym.ObservationWrapper):
    def __init__(self, env=None):
        super(NormalizedEnv, self).__init__(env)
        self.state_mean = 0
        self.state_std = 0
        self.alpha = 0.9999
        self.num_steps = 0

    def observation(self, observation):
        self.num_steps += 1
        self.state_mean = self.state_mean * self.alpha + \
            observation.mean() * (1 - self.alpha)
        self.state_std = self.state_std * self.alpha + \
            observation.std() * (1 - self.alpha)

        unbiased_mean = self.state_mean / (1 - pow(self.alpha, self.num_steps))
        unbiased_std = self.state_std / (1 - pow(self.alpha, self.num_steps))

        return (observation - unbiased_mean) / (unbiased_std + 1e-8)

def PrimaryAtariWrap(env,clip_rewards=True):
    env = ClipRewardEnv(env)
    env = PreprocessAtariObs(env)    # 이미지 전처리
    env = NormalizedEnv(env)
    return env

# %%
def make_env(env_name='PongDeterministic-v4',seed=None):
    env = gym.make(env_name) 
    if seed is not None:
        env.seed(seed)
    env = ClipRewardEnv(env)
    env = PreprocessAtariObs(env)    # 이미지 전처리
    env = NormalizedEnv(env)
    return env

# %%
class LSTM_A2C_Agent(nn.Module):
    def __init__(self,num_actions):
        super(LSTM_A2C_Agent,self).__init__()
        self.seq = nn.Sequential(
            nn.Conv2d(1, 32, 3, stride=2, padding=1),
            nn.ELU(),
            nn.Conv2d(32, 32, 3, stride=2, padding=1),
            nn.ELU(),
            nn.Conv2d(32, 32, 3, stride=2, padding=1),
            nn.ELU(),
            nn.Conv2d(32, 32, 3, stride=2, padding=1),
            nn.ELU(),
            nn.Flatten(),
        )
        self.lstm = nn.LSTMCell(32 * 3 * 3, 256) 
        self.policy = nn.Linear(256,num_actions)
        self.value = nn.Linear(256,1)

    def forward(self, state_t):
        '''
        입력인자
            state_t : 상태([batch,state_shape]), torch.tensor
        출력인자
            policy : 정책([batch,n_actions]), torch.tensor
            value : 가치함수([batch]), torch.tensor
        '''
        state_t, (hx,cx) = state_t
        state_t = self.seq(state_t)
        #print(f'shape: {state_t.shape}')
        hx, cx = self.lstm(state_t, (hx, cx))
        state_t = hx
        policy = self.policy(state_t)
        value = self.value(state_t)
        return policy, value, (hx,cx)
    
    def sample_actions(self,state_t):
        '''
        입력인자
            state_t : 상태([1,state_shape]), torch.tensor
        출력인자
            action_t : 행동함수 using torch.multinomial
        '''
        #state_t,(hx,cx) = state_t
        #state_t = self.seq(state_t)
        #policy, _, _ = self.forward(state_t,(hx,cx))
        policy, _, _ = self.forward(state_t)
        policy = torch.squeeze(policy)
        softmax_policy = F.softmax(policy,dim=0)
        action = torch.multinomial(softmax_policy, num_samples=1).item()
        return action

# %%
def LSTM_A2C_loss(transition,train_agent,env,gamma=0.99,epsilon=1e-02):
    '''
    A3C loss함수 계산코드
    입력인자
        batch_sample - 리플레이로부터 받은 샘플(S,A,R,S',done, LSTM_gate)
        train_agent - 훈련에이전트
        env - 환경
        gamma - 할인율
        epsilon - entropy 가중치
    출력인자
        Total_loss
    목적함수 
        -log(policy)*advantage + (value_infer-value_target)**2 + policy*log(policy)
        Actor-loss(exploitation): "log(policy)*advantage"
        Actor-entropy(exploration): "policy*log(policy)"
        Critic-loss: "MSE(value_infer - value_target)"
    '''
    states,actions,rewards,next_states,dones,lstm_gate = transition
    hx_present, cx_present, hx_next, cx_next = lstm_gate 
    
    # hx, cx definition
    hx_present = torch.Tensor(hx_present).to(device)
    cx_present = torch.Tensor(cx_present).to(device)
    hx_next = torch.Tensor(hx_next).to(device)
    cx_next = torch.Tensor(cx_next).to(device)

    states = torch.Tensor(states).to(device)
    actions = torch.LongTensor(actions).to(device)
    rewards = torch.Tensor(rewards).to(device)
    next_states = torch.Tensor(next_states).to(device)
    #policies, values = train_agent(states)
    #_, next_values = train_agent(next_states)
    # LSTM forward propagation
    states = (states,(hx_present,cx_present))
    next_states = (next_states,(hx_next,cx_next))
    #policies, values, _, _ = train_agent(states, hx_present, cx_present)
    #_, next_values, _, _ = train_agent(states, hx_next, cx_next)
    policies, values, _ = train_agent(states)
    _, next_values, _ = train_agent(next_states)

    probs = F.softmax(policies,dim=-1)
    logprobs = F.log_softmax(policies,dim=-1)
    logp_actions = logprobs[np.arange(states[0].shape[0]),actions]
    entropy = -torch.sum(probs*logprobs,dim=-1)

    # Calculate loss function from backstep
    R = 0
    if dones[-1] != False:
        R = next_values[-1].item()
    values = torch.cat((values,torch.Tensor([[R]]).to(device)),dim=0)

    actor_loss = 0
    critic_loss = 0
    advantage, gen_delta = 0, 0
    for i in reversed(range(len(rewards))):
        R = gamma * R + rewards[i]
        advantage = R - values[i]
        critic_loss +=  advantage.pow(2)

        # Generalized Advantage Estimation
        delta_t = rewards[i]+gamma*values[i+1]-values[i]
        gen_delta = gamma*gen_delta+delta_t
        actor_loss -= logp_actions[i]*gen_delta.detach()+epsilon*entropy[i]

    total_loss = actor_loss + critic_loss
    return total_loss, actor_loss, critic_loss

def train(rank,args,shared_agent):
    env = make_env(args.env_name,args.seed+rank)
    optimizer = optim.Adam(shared_agent.parameters(),lr=args.lr)
    
    A3C_record = []
    reward_record = [] 
    start = time.time()
    for ep in range(args.max_episode_length):
        done = False
        state = env.reset()
        #print(f'Initial shape:{state.shape}')
        total_reward = 0 

        
        hx = torch.zeros(1,256).to(device)
        cx = torch.zeros(1,256).to(device)
        while not done:
            states,actions,rewards,next_states,dones = [],[],[],[],[]
            hx_present, cx_present,hx_next, cx_next = [],[],[],[]
            for step in range(args.num_steps):
                # hx_present, cx_present append
                hx_present.append(hx.squeeze().detach().cpu().numpy())
                cx_present.append(cx.squeeze().detach().cpu().numpy())

                torch_state = torch.Tensor(state).to(device)
                torch_state = torch.unsqueeze(torch_state,0)
                
                torch_state = (torch_state,(hx,cx))
                #_,_,hx,cx = shared_agent(torch_state,hx,cx)
                _,_,(hx,cx) = shared_agent(torch_state)
                #action = shared_agent.sample_actions(torch_state,hx,cx)
                action = shared_agent.sample_actions(torch_state)
                next_state,reward,done,_ = env.step(action)
                total_reward += reward
                
                states.append(state)
                actions.append(action)
                rewards.append(reward)
                next_states.append(next_state)
                dones.append(done)

                # hx_next, cx_next concatenate
                hx_next.append(hx.squeeze().detach().cpu().numpy())
                cx_next.append(cx.squeeze().detach().cpu().numpy())

                # 업데이트
                state = next_state
                if done:
                    if total_reward == 20:
                        best_agent = copy.deepcopy(shared_agent)
                    break
            
            LSTM_gate = (hx_present, cx_present, hx_next, cx_next)
            transition = (states,actions,rewards,next_states,dones,LSTM_gate)
            loss,actor_loss,critic_loss = LSTM_A2C_loss(transition,shared_agent,env,args.gamma,args.entropy_coef)
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(shared_agent.parameters(),args.max_grad_norm)
            optimizer.step()

        reward_record.append(total_reward)
        finish = time.time()-start
        if rank == 0 and ep % 100==0:
            A3C_record.append([finish,total_reward]) 

        # Training log
        print(f'|프로세스|{rank}|   >>>   Reward/Episode: {total_reward}/{ep}   Time: {time.strftime("%Hh %Mm %Ss",time.gmtime(finish))}') 
        if np.mean(reward_record[-3:]) >= 15 and total_reward >= 15:
            best_agent = copy.deepcopy(shared_agent)
            print(f" 프로세스: {rank}   >>>   보상: {np.mean(reward_record[-3:])}")
            print(f"학습종료")
            if rank == 1:
                np.save('./A3C_timeVSreward.npy',np.array(A3C_record))
            break 

def test(rank,args,shared_agent,test_games=3): 
    env = make_env(args.env_name,args.seed+rank)

    reward_record = [] 
    for ep in range(test_games):
        done = False
        state = env.reset()
        total_reward = 0 

        hx = torch.zeros(1,256).to(device)
        cx = torch.zeros(1,256).to(device)
        while True:
            torch_state = torch.Tensor(state).to(device)
            torch_state = torch.unsqueeze(torch_state,0)
            #action = shared_agent.sample_actions(torch_state)
            torch_state = (torch_state,(hx,cx))
            _,_,(hx,cx) = shared_agent(torch_state)
            action = shared_agent.sample_actions(torch_state)
            next_state,reward,done,_ = env.step(action)
            total_reward += reward
            
            # 업데이트
            state = next_state
            if done:
                break

        reward_record.append(total_reward)
    print(f'{test_games} 게임')
    print(f'        >> 평균보상: {np.mean(reward_record)}')
    print(f'        >> 최대보상: {np.max(reward_record)}')

if __name__ == '__main__':
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    env = make_env() 
    n_actions = env.action_space.n
    train_agent = LSTM_A2C_Agent(n_actions).to(device)

    train(0,args,train_agent)
    test(0,args,train_agent,3) 
    torch.save(train_agent,'./ckpt/PongDeterministic/Pong_best_agent_LSTM_A2C.pth')
