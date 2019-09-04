import os
import gym
from gym import spaces
from numpy.random import random
import numpy as np
import gym_auv.utils.geomutils as geom

from gym.utils import seeding, EzPickle
from gym_auv.rendering import render_env, init_env_viewer, FPS

import matplotlib
matplotlib.rcParams['hatch.linewidth'] = 0.5
import matplotlib.pyplot as plt
plt.style.use('ggplot')

class BaseShipScenario(gym.Env):
    """Creates an environment with a vessel and a path.
    
    Attributes:
        config : dict
            The configuration disctionary specifying rewards,
            look ahead distance, simulation timestep and desired cruise
            speed.
        nsectors : int
            The number of obstacle sectors.
        nstates : int
            The number of state variables passed to the agent.
        vessel : gym_auv.objects.auv.AUV2D
            The AUV that is controlled by the agent.
        path : gym_auv.objects.path.RandomCurveThroughOrigin
            The path to be followed.
        np_random : np.random.RandomState
            Random number generator.
        reward : float
            The accumulated reward
        path_prog : float
            Progression along the path in terms of arc length covered.
        past_actions : np.array
            All actions that have been perfomed.
        action_space : gym.spaces.Box
            The action space. Consists of two floats that must take on
            values between -1 and 1.
        observation_space : gym.spaces.Box
            The observation space. Consists of
            self.nstates + self.nsectors*2 floats that must be between
            0 and 1.
    
    Raises:
        NotImplementedError: Method is not implemented.
    """

    metadata = {
        'render.modes': ['human', 'rgb_array', 'state_pixels'],
        'video.frames_per_second': FPS
    }

    def __init__(self, env_config):
        """
        The __init__ method declares all class atributes and calls
        the self.reset() to intialize them properly.

        Parameters
        ----------
        env_config : dict
            Configuration parameters for the environment.
            Must have the following members:
            reward_ds
                The reward for progressing ds along the path in
                one timestep. reward += reward_ds*ds.
            reward_speed_error
                reward += reward_speed_error*speed_error where the
                speed error is abs(speed-cruise_speed)/max_speed.
            reward_cross_track_error
                reward += reward_cross_track_error*cross_track_error
            la_dist
                The look ahead distance.
            t_step_size
                The timestep
            cruise_speed
                The desired cruising speed.
        """
        self.config = env_config
        self.nstates = 7
        self.nsectors = self.config["n_sectors"]
        self.nsensors = self.config["n_sensors_per_sector"]*self.config["n_sectors"]
        self.sensor_angles = [-np.pi/2 + (i + 1)/(self.nsensors + 1)*np.pi for i in range(self.nsensors)]

        self.sensor_obst_intercepts = [None for isensor in range(self.nsensors)]
        self.obst_active_sensors = [None for isector in range(self.nsectors)]
        self.sensor_obst_measurements = np.zeros((self.nsensors, ))
        self.sensor_path_arclengths = np.zeros((self.nsensors, ))
        self.sensor_path_index = None
        self.sensor_order = range(self.nsensors)
        self.vessel = None
        self.path = None
        self.obstacles = None
        self.nearby_obstacles = None
        self.look_ahead_point = None
        self.look_ahead_arclength = None

        self.np_random = None

        self.cumulative_reward = 0
        self.past_rewards = None
        self.max_path_prog = None
        self.target_arclength = None
        self.path_prog = None
        self.past_actions = None
        self.past_obs = None
        self.past_errors = None
        self.t_step = 0
        self.total_t_steps = 0
        self.episode = 0
        self.memory = []

        init_env_viewer(self)

        self.reset()

        self.action_space = gym.spaces.Box(
            low=np.array([0, -1]),
            high=np.array([1, 1]),
            dtype=np.float32
        )
        nobservations = self.nstates + self.nsectors*2
        low_obs = [-1]*nobservations
        high_obs = [1]*nobservations
        low_obs[self.nstates - 1] = -10000
        high_obs[self.nstates - 1] = 10000
        self.observation_space = gym.spaces.Box(
            low=np.array(low_obs),
            high=np.array(high_obs),
            dtype=np.float32
        )
        

    def step(self, action):
        """
        Simulates the environment for one timestep when action
        is performed

        Parameters
        ----------
        action : np.array
            [propeller_input, rudder_position].
        Returns
        -------
        obs : np.array
            Observation of the environment after action is performed.
        step_reward : double
            The reward for performing action at his timestep.
        done : bool
            If True the episode is ended.
        info : dict
            Empty, is included because it is required of the
            OpenAI Gym frameowrk.
        """
        self.past_actions = np.vstack([self.past_actions, action])
        self.vessel.step(action)

        closest_point_distance, _, closest_arclength = self.path.get_closest_point_distance(self.vessel.position)
        closest_point_heading_error = geom.princip(self.path.get_direction(closest_arclength) - self.vessel.course)
        course_path_angle = geom.princip(self.path.get_direction(self.max_path_prog) - self.vessel.course)
        dprog = np.cos(course_path_angle) * self.vessel.speed * self.config["t_step_size"]
        if (
            closest_point_distance < self.config["max_closest_point_distance"] or
            abs(closest_point_heading_error) < self.config["max_closest_point_heading_error"]
        ):
            prog = closest_arclength
        else:
            prog = min(max(0, self.max_path_prog + dprog), self.path.length)

        if prog > self.max_path_prog:
            self.max_path_prog = prog

        self.path_prog = np.append(self.path_prog, prog)

        if (self.look_ahead_arclength is None):
            self.target_arclength = self.max_path_prog + self.config["min_la_dist"]
        else:
            self.target_arclength = max(self.look_ahead_arclength, self.max_path_prog + self.config["min_la_dist"])
        self.target_arclength = min(self.target_arclength, self.path.length)

        obs = self.observe()
        assert not np.isnan(obs).any(), 'Observation vector "{}" contains nan values.'.format(str(obs))
        self.past_obs = np.vstack([self.past_obs, obs])
        done, step_reward, info = self.step_reward()
        info['progress'] = prog/self.path.length
        self.past_rewards = np.append(self.past_rewards, step_reward)
        self.cumulative_reward += step_reward

        self.t_step += 1
        self.total_t_steps += 1
        if (self.t_step > self.config["max_timestemps"]):
            done = True

        return obs, step_reward, done, info

    def reset(self):
        """
        Resets the environment by reseeding and calling self.generate.

        Returns
        -------
        obs : np.array
            The initial observation of the environment.
        """

        if (self.t_step > 0):
            self.memory = {
                'path': self.path(np.linspace(0, self.path.s_max, 1000)),
                'path_taken': self.vessel.path_taken
            }

        self.vessel = None
        self.path = None
        self.cumulative_reward = 0
        self.max_path_prog = 0
        self.target_arclength = 0
        self.path_prog = None
        self.past_obs = None
        self.past_actions = np.array([[0, 0]])
        self.past_rewards = np.array([])
        self.past_errors = {
            'speed': np.array([]),
            'cross_track': np.array([]),
            'heading': np.array([]),
            'la_heading': np.array([]),
        }
        self.obstacles = []
        self.nearby_obstacles = []
        self.t_step = 0
        self.look_ahead_point = None
        self.look_ahead_arclength = None

        if self.np_random is None:
            self.seed()

        self.generate()
        obs = self.observe()
        assert not np.isnan(obs).any(), 'Observation vector "{}" contains nan values.'.format(str(obs))
        self.past_obs = np.array([obs])
        self.episode += 1
        return obs

    def close(self):
        self.viewer.close()

    def generate(self):
        raise NotImplementedError()

    def observe(self):
        raise NotImplementedError()

    def step_reward(self):
        raise NotImplementedError()

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def render(self, mode='human'):
        image_arr = render_env(self, mode)
        return image_arr

    def plot(self, fig_dir, fig_name):
        """
        Plots the result of a path following episode.

        Parameters
        ----------
        fig_dir : str
            Absolute path to a directory to store the plotted
            figure in.
        fig_name : str
            Name of figure.
        """

        path = self.memory['path']
        path_taken = self.memory['path_taken']

        plt.axis('scaled')
        fig_path = plt.figure()
        ax_path = fig_path.add_subplot(1, 1, 1)
        ax_path.set_aspect('equal')

        # for obst in self.obstacles:
        #     ax_path.add_patch(plt.Circle(obst.position[::-1],
        #                                 (obst.radius
        #                                 + self.config["obst_detection_range"]),
        #                                 facecolor='tab:blue',
        #                                 edgecolor='tab:blue',
        #                                 alpha=0.2,
        #                                 linewidth=0.5))
        #     ax_path.add_patch(plt.Circle(obst.position[::-1],
        #                                 (obst.radius
        #                                 + self.config["obst_reward_range"]),
        #                                 facecolor='tab:red',
        #                                 edgecolor='tab:red',
        #                                 alpha=0.4,
        #                                 linewidth=0.5))
        for obst in self.obstacles:
            obst = ax_path.add_patch(plt.Circle(obst.position[::-1],
                                                obst.radius,
                                                facecolor='tab:red',
                                                edgecolor='black',
                                                linewidth=0.5))
            obst.set_hatch('////')

        ax_path.plot(path[1, :], path[0, :], dashes=[6, 2], color='black', linewidth=1.5, label=r'Path')
        ax_path.plot(path_taken[:, 1], path_taken[:, 0], color='tab:blue', label=r'Path taken')
        ax_path.set_ylabel(r"North (m)")
        ax_path.set_xlabel(r"East (m)")
        ax_path.legend()

        fig_path.savefig(fig_dir + '/' + fig_name + '.pdf', format='pdf')