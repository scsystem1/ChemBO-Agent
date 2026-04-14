"""
Local SMKBO kernel adaptation for ChemBO-Agent.

This module keeps the spectral-mixture-based continuous kernel pieces from the
prototype in ../SMK while making them importable from the current package.
"""
from __future__ import annotations

import math
from typing import Optional, Union

import torch
from gpytorch.constraints import Positive
from gpytorch.kernels import AdditiveKernel, Kernel, RBFKernel
from gpytorch.kernels.spectral_mixture_kernel import SpectralMixtureKernel


def _wrap_integer_dims(x1: torch.Tensor, x2: torch.Tensor, integer_dims: list[int] | None):
    """Round integer-valued continuous dimensions before kernel evaluation."""
    if integer_dims is None:
        return x1, x2
    x1_wrapped = x1.clone()
    x2_wrapped = x2.clone()
    for dim in integer_dims:
        x1_wrapped[:, dim] = torch.round(x1_wrapped[:, dim])
        x2_wrapped[:, dim] = torch.round(x2_wrapped[:, dim])
    return x1_wrapped, x2_wrapped


class CauchyMixtureKernel(Kernel):
    """Heavy-tailed spectral mixture kernel using Cauchy spectral components."""

    is_stationary = True

    def __init__(
        self,
        num_mixtures: Optional[int] = None,
        ard_num_dims: Optional[int] = 1,
        batch_shape: Optional[torch.Size] = torch.Size([]),
        **kwargs,
    ):
        if num_mixtures is None:
            raise RuntimeError("num_mixtures is a required argument")

        super().__init__(ard_num_dims=ard_num_dims, batch_shape=batch_shape, **kwargs)
        self.num_mixtures = num_mixtures

        self.register_parameter(
            name="raw_mixture_weights",
            parameter=torch.nn.Parameter(torch.zeros(*self.batch_shape, self.num_mixtures)),
        )
        parameter_shape = torch.Size([*self.batch_shape, self.num_mixtures, 1, self.ard_num_dims])
        self.register_parameter(name="raw_mixture_means", parameter=torch.nn.Parameter(torch.zeros(parameter_shape)))
        self.register_parameter(name="raw_mixture_scales", parameter=torch.nn.Parameter(torch.zeros(parameter_shape)))

        positive = Positive()
        self.register_constraint("raw_mixture_scales", positive)
        self.register_constraint("raw_mixture_means", positive)
        self.register_constraint("raw_mixture_weights", positive)

    @property
    def mixture_scales(self):
        return self.raw_mixture_scales_constraint.transform(self.raw_mixture_scales)

    @mixture_scales.setter
    def mixture_scales(self, value: Union[torch.Tensor, float]):
        if not torch.is_tensor(value):
            value = torch.as_tensor(value).to(self.raw_mixture_scales)
        self.initialize(raw_mixture_scales=self.raw_mixture_scales_constraint.inverse_transform(value))

    @property
    def mixture_means(self):
        return self.raw_mixture_means_constraint.transform(self.raw_mixture_means)

    @mixture_means.setter
    def mixture_means(self, value: Union[torch.Tensor, float]):
        if not torch.is_tensor(value):
            value = torch.as_tensor(value).to(self.raw_mixture_means)
        self.initialize(raw_mixture_means=self.raw_mixture_means_constraint.inverse_transform(value))

    @property
    def mixture_weights(self):
        return self.raw_mixture_weights_constraint.transform(self.raw_mixture_weights)

    @mixture_weights.setter
    def mixture_weights(self, value: Union[torch.Tensor, float]):
        if not torch.is_tensor(value):
            value = torch.as_tensor(value).to(self.raw_mixture_weights)
        self.initialize(raw_mixture_weights=self.raw_mixture_weights_constraint.inverse_transform(value))

    def initialize_from_data(self, train_x: torch.Tensor, train_y: torch.Tensor, **kwargs):
        """Use the original heuristic initialization from the prototype kernel."""
        with torch.no_grad():
            if train_x.ndimension() == 1:
                train_x = train_x.unsqueeze(-1)
            if self.active_dims is not None:
                train_x = train_x[..., self.active_dims]

            train_x_sort = train_x.sort(dim=-2)[0]
            max_dist = train_x_sort[..., -1, :] - train_x_sort[..., 0, :]
            dists = train_x_sort[..., 1:, :] - train_x_sort[..., :-1, :]
            dists = torch.where(
                dists.eq(0.0),
                torch.tensor(1.0e10, dtype=train_x.dtype, device=train_x.device),
                dists,
            )
            min_dist = dists.sort(dim=-2)[0][..., 0, :]

            min_dist = min_dist.unsqueeze(-2).unsqueeze(-3)
            max_dist = max_dist.unsqueeze(-2).unsqueeze(-3)
            dim = -3
            while -dim <= min_dist.dim():
                if -dim > self.raw_mixture_scales.dim():
                    min_dist = min_dist.min(dim=dim)[0]
                    max_dist = max_dist.max(dim=dim)[0]
                elif self.raw_mixture_scales.size(dim) == 1:
                    min_dist = min_dist.min(dim=dim, keepdim=True)[0]
                    max_dist = max_dist.max(dim=dim, keepdim=True)[0]
                    dim -= 1
                else:
                    dim -= 1

            self.mixture_scales = torch.randn_like(self.raw_mixture_scales).mul_(max_dist).abs_().reciprocal_()
            self.mixture_means = torch.rand_like(self.raw_mixture_means).mul_(0.5).div(min_dist)
            self.mixture_weights = train_y.std().div(self.num_mixtures)

    def _create_input_grid(self, x1: torch.Tensor, x2: torch.Tensor, diag: bool = False, **params):
        if diag:
            return x1, x2
        return x1.unsqueeze(-2), x2.unsqueeze(-3)

    def forward(self, x1: torch.Tensor, x2: torch.Tensor, diag: bool = False, **params):
        _, num_dims = x1.shape[-2:]
        if num_dims != self.ard_num_dims:
            raise RuntimeError(
                f"The CauchyMixtureKernel expected input dim {self.ard_num_dims}, got {num_dims}."
            )

        x1_ = x1.unsqueeze(-3)
        x2_ = x2.unsqueeze(-3)
        x1_exp = x1_ * self.mixture_scales
        x2_exp = x2_ * self.mixture_scales
        x1_cos = x1_ * self.mixture_means
        x2_cos = x2_ * self.mixture_means

        x1_exp_, x2_exp_ = self._create_input_grid(x1_exp, x2_exp, diag=diag, **params)
        x1_cos_, x2_cos_ = self._create_input_grid(x1_cos, x2_cos, diag=diag, **params)

        exp_term = (x1_exp_ - x2_exp_).abs_().mul_(-2 * math.pi)
        cos_term = (x1_cos_ - x2_cos_).mul_(2 * math.pi)
        result = exp_term.exp_() * cos_term.cos_()

        mixture_weights = self.mixture_weights.view(*self.mixture_weights.shape, 1, 1)
        if not diag:
            mixture_weights = mixture_weights.unsqueeze(-2)
        result = (result * mixture_weights).sum(-3 if diag else -4)
        return result.prod(-1)


class SMKKernel(AdditiveKernel):
    """SMKBO continuous kernel: Gaussian SM + Cauchy SM additive composition."""

    def __init__(self, num_mixtures1: int = 5, num_mixtures2: int = 4, **kwargs):
        super().__init__()
        self.kernels_list = []

        self.smk1 = None
        if num_mixtures1 > 0:
            self.smk1 = SpectralMixtureKernel(num_mixtures=num_mixtures1, **kwargs)
            self.kernels_list.append(self.smk1)

        self.smk2 = None
        if num_mixtures2 > 0:
            self.smk2 = CauchyMixtureKernel(num_mixtures=num_mixtures2, **kwargs)
            self.kernels_list.append(self.smk2)

        self.fallback_kernel = None
        if not self.kernels_list:
            self.fallback_kernel = RBFKernel(**kwargs)
            self.kernels_list.append(self.fallback_kernel)

    @property
    def kernels(self):
        return self.kernels_list

    def initialize_from_data(self, train_x: torch.Tensor, train_y: torch.Tensor):
        for kernel in self.kernels_list:
            if hasattr(kernel, "initialize_from_data"):
                kernel.initialize_from_data(train_x, train_y)


class WrappedSMK(SMKKernel):
    """SMKKernel variant that rounds integer-valued dimensions before evaluation."""

    def __init__(self, integer_dims: list[int] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.integer_dims = integer_dims

    def forward(self, x1, x2, diag: bool = False, **params):
        x1_wrapped, x2_wrapped = _wrap_integer_dims(x1, x2, self.integer_dims)
        return super().forward(x1_wrapped, x2_wrapped, diag=diag, **params)
