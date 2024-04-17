# Copyright 2023 OmniSafe Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Implementation of the Control Barrier Function Solver."""

from __future__ import annotations
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import torch
from cvxopt import matrix
from cvxopt import solvers
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF, ConstantKernel as C

class PendulumSolver:
    """Solver for the pendulum problem using Gaussian Process models.

    Attributes:
        action_size (int): Size of the action space.
        observation_size (int): Size of the observation space.
        torque_bound (float): Maximum torque bound.
        max_speed (float): Maximum speed of the pendulum.
        device (str): Device to run the computations on.
    """

    def __init__(self, action_size: int = 1, observation_size: int = 3, 
                 torque_bound: float = 15., max_speed: float = 60., 
                 device: str = 'cpu') -> None:
        """Initializes the PendulumSolver with specified parameters.

        Args:
            action_size (int): Size of the action space.
            observation_size (int): Size of the observation space.
            torque_bound (float): Maximum torque bound.
            max_speed (float): Maximum speed of the pendulum.
            device (str): Device to run the computations on.
        """
        self.action_size = action_size
        self.observation_size = observation_size
        self.torque_bound = torque_bound
        self.max_speed = max_speed
        self.F = 1.0
        self._device = device
        self._gamma_b = 0.5
        self._kd = 1.5
        self._build_barrier()
        self.build_GP_model()
        self.GP_model_prev = None

    def build_GP_model(self) -> None:
        """Builds the Gaussian Process model."""
        gp_list = []
        noise = 0.01
        for _ in range(self.observation_size - 1):
            kern = C(1.0, (1e-3, 1e3)) * RBF(10, (1e-2, 1e2))
            gp = GaussianProcessRegressor(kernel=kern, alpha=noise, n_restarts_optimizer=10)
            gp_list.append(gp)
        self.GP_model = gp_list

    def _build_barrier(self) -> None:
        """Builds the barrier for the pendulum solver."""
        self.P = matrix(np.diag([1., 1e16]), tc='d')
        self.q = matrix(np.zeros(self.action_size + 1))
        self.h1 = np.array([1, 0.01])
        self.h2 = np.array([1, -0.01])
        self.h3 = np.array([-1, 0.01])
        self.h4 = np.array([-1, -0.01])

    def control_barrier(self, original_action: torch.Tensor, f: np.ndarray, g: np.ndarray, x: np.ndarray, std: np.ndarray) -> torch.Tensor:
        """
        Adjusts the original action using a control barrier function to ensure
        that the action complies with the system's physical constraints.

        Args:
            original_action (torch.Tensor): The original action proposed by the RL algorithm.
            f (np.ndarray): The drift component of the system's dynamics.
            g (np.ndarray): The control component of the system's dynamics.
            x (np.ndarray): The current state of the system.
            std (np.ndarray): The standard deviation of the system's state.

        Returns:
            torch.Tensor: The adjusted action that respects the system's constraints.
        """

        # Define gamma for the barrier function
        gamma_b = 0.5
        kd = 1.5
        u_rl = original_action.detach().numpy()
        # u_rl*=self.torque_bound

        # Set up Quadratic Program to satisfy Control Barrier Function
        G = np.array(
            [
                [
                    -np.dot(self.h1, g), 
                    -np.dot(self.h2, g), 
                    -np.dot(self.h3, g), 
                    -np.dot(self.h4, g), 
                    1,
                    -1, 
                    g[1], 
                    -g[1]
                ], 
                [
                    -1, 
                    -1, 
                    -1, 
                    -1, 
                    0, 
                    0, 
                    0, 
                    0
                ]
            ]
        )
        G = np.transpose(G)
        h = np.array(
            [
                gamma_b * self.F + np.dot(self.h1, f) + np.dot(self.h1, g) * u_rl - (1 - gamma_b) * np.dot(self.h1, x) - kd * np.abs(np.dot(self.h1, std)),
                    gamma_b * self.F + np.dot(self.h2, f) + np.dot(self.h2, g) * u_rl - (1 - gamma_b) * np.dot(self.h2, x) - kd * np.abs(np.dot(self.h2, std)),
                    gamma_b * self.F + np.dot(self.h3, f) + np.dot(self.h3, g) * u_rl - (1 - gamma_b) * np.dot(self.h3, x) - kd * np.abs(np.dot(self.h3, std)),
                    gamma_b * self.F + np.dot(self.h4, f) + np.dot(self.h4, g) * u_rl - (1 - gamma_b) * np.dot(self.h4, x) - kd * np.abs(np.dot(self.h4, std)),
                    -u_rl + self.torque_bound,
                    u_rl + self.torque_bound,
                    -f[1] - g[1] * u_rl + self.max_speed,
                    f[1] + g[1] * u_rl + self.max_speed
            ]
        )
        h = np.squeeze(h).astype(np.double)
        
        # Convert numpy arrays to cvx matrices to set up QP
        G = matrix(G, tc='d')
        h = matrix(h, tc='d')
        solvers.options['show_progress'] = False
        sol = solvers.qp(self.P, self.q, G, h)
        u_bar = sol['x']

        # Check if the adjusted action is within bounds
        if np.add(np.squeeze(u_rl), np.squeeze(u_bar[0])) - 0.001 >= self.torque_bound:
            u_bar[0] = self.torque_bound - u_rl
            print("Error in QP")
        elif np.add(np.squeeze(u_rl), np.squeeze(u_bar[0])) + 0.001 <= -self.torque_bound:
            u_bar[0] = -self.torque_bound - u_rl
            print("Error in QP")

        return torch.as_tensor(u_bar[0], dtype=torch.float32, device=self._device).unsqueeze(dim=0)

    def get_dynamics(self, obs: list[float], original_action: float) -> np.ndarray:
        """
        Calculates the dynamics of the system based on the current observation
        and the original action.

        Args:
            obs (list[float]): The current observation of the system state.
            original_action (float): The original action proposed by the RL algorithm.

        Returns:
            np.ndarray: The calculated dynamics of the system.
        """

        dt = 0.05  # Time step
        G = 10  # Gravitational constant
        m = 2  # Mass
        l = 2  # Length

        theta = np.arctan2(obs[1], obs[0])  # Calculate the angle
        theta_dot = obs[2]  # Angular velocity

        # Dynamics equations
        f = np.array([-3 * G / (2 * l) * np.sin(theta + np.pi) * dt**2 + theta_dot * dt + theta + 3 / (m * l**2) * original_action * dt**2,
                    theta_dot - 3 * G / (2 * l) * np.sin(theta + np.pi) * dt + 3 / (m * l**2) * original_action * dt])

        return np.squeeze(f)

    def update_GP_dynamics(self, obs: np.ndarray, act: np.ndarray) -> None:
        """
        Updates the Gaussian Process (GP) dynamics model based on observed states and actions.

        Args:
            obs (np.ndarray): Observed states.
            act (np.ndarray): Actions taken.
        """
        obs=obs.detach().cpu().squeeze().numpy()
        act=act.detach().cpu().squeeze().numpy()
        N = self.observation_size
        X = obs
        U = act
        L = len(X)
        err = np.zeros((L-1, N-1))
        S = np.zeros((L-1, 2))
        for i in range(L-1):
            f = self.get_dynamics(X[i], U[i])
            theta_p = np.arctan2(X[i][1], X[i][0])
            theta_dot_p = X[i][2]
            theta = np.arctan2(X[i+1][1], X[i+1][0])
            theta_dot = X[i+1][2]
            S[i, :] = np.array([theta_p, theta_dot_p])
            err[i, :] = np.array([theta, theta_dot]) - f
        self.GP_model[0].fit(S, err[:, 0])
        self.GP_model[1].fit(S, err[:, 1])

    def get_GP_dynamics(self, obs: torch.Tensor, use_prev_model: bool) -> list[np.ndarray]:
        """
        Retrieves the GP dynamics based on the current observation.

        Args:
            obs (torch.Tensor): Current state observation.

        Returns:
            list[np.ndarray]: list containing the GP dynamics [f, g, x, std].
        """
        obs = obs.cpu().detach().numpy()
        u_rl = 0
        dt = 0.05
        G = 10
        m = 1
        l = 1
        obs = np.squeeze(obs)
        theta = np.arctan2(obs[1], obs[0])
        theta_dot = obs[2]
        x = np.array([theta, theta_dot]) # 这个x估计就对应state
        f_nom = np.array(
            [
                -3*G/(2*l)*np.sin(theta + np.pi)*dt**2 + theta_dot*dt + theta + 3/(m*l**2)*u_rl*dt**2, 
                theta_dot - 3*G/(2*l)*np.sin(theta + np.pi)*dt + 3/(m*l**2)*u_rl*dt
            ]
        )
        g = np.array([3/(m*l**2)*dt**2, 3/(m*l**2)*dt])
        f_nom = np.squeeze(f_nom)
        f = np.zeros(2)
        if use_prev_model:
            [m1, std1] = self.GP_model_prev[0].predict(x.reshape(1,-1), return_std=True)
            [m2, std2] = self.GP_model_prev[1].predict(x.reshape(1,-1), return_std=True)
        else:
            [m1, std1] = self.GP_model[0].predict(x.reshape(1, -1), return_std=True)
            [m2, std2] = self.GP_model[1].predict(x.reshape(1, -1), return_std=True)
        f[0] = f_nom[0] + m1
        f[1] = f_nom[1] + m2
        return [np.squeeze(f), np.squeeze(g), np.squeeze(x), np.array([np.squeeze(std1), np.squeeze(std2)])]
