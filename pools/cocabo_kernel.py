"""
CoCaBO mixed kernel for GPyTorch/BoTorch.

Reference:
    Ru et al., "Bayesian Optimisation over Multiple Continuous and
    Categorical Inputs", ICML 2020.

Kernel form:
    kz = (1 - lambda) * (kh + kx) + lambda * kh * kx

where kh is an overlap/indicator kernel on categorical variables and kx is a
standard continuous kernel. The mixing coefficient is learned jointly with the
GP hyperparameters through the marginal likelihood.
"""
from __future__ import annotations

from typing import Any

import gpytorch
import torch


class IndicatorKernel(gpytorch.kernels.Kernel):
    """Average indicator kernel over categorical dimensions."""

    is_stationary = False

    def __init__(self, cat_dims: list[int], **kwargs: Any):
        super().__init__(**kwargs)
        self.cat_dims = list(cat_dims)

    def forward(
        self,
        x1: torch.Tensor,
        x2: torch.Tensor,
        last_dim_is_batch: bool = False,
        **params: Any,
    ) -> torch.Tensor:
        del last_dim_is_batch, params
        n = x1.shape[-2]
        m = x2.shape[-2]
        if not self.cat_dims:
            return torch.ones(*x1.shape[:-2], n, m, dtype=x1.dtype, device=x1.device)

        x1_cat = x1[..., self.cat_dims]
        x2_cat = x2[..., self.cat_dims]
        matches = (x1_cat.unsqueeze(-2) == x2_cat.unsqueeze(-3)).to(dtype=x1.dtype)
        return matches.mean(dim=-1)


class CoCaBOMixedKernel(gpytorch.kernels.Kernel):
    """CoCaBO kernel combining categorical indicator and continuous kernels."""

    is_stationary = False

    def __init__(
        self,
        *,
        cont_dims: list[int],
        cat_dims: list[int],
        base_cont_kernel: gpytorch.kernels.Kernel,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.cont_dims = list(cont_dims)
        self.cat_dims = list(cat_dims)
        self.cont_kernel = base_cont_kernel
        self.cat_kernel = IndicatorKernel(self.cat_dims)
        self.register_parameter("raw_lambda", torch.nn.Parameter(torch.zeros(1)))

    @property
    def lambda_val(self) -> torch.Tensor:
        return torch.sigmoid(self.raw_lambda)

    def forward(
        self,
        x1: torch.Tensor,
        x2: torch.Tensor,
        last_dim_is_batch: bool = False,
        **params: Any,
    ) -> torch.Tensor:
        del last_dim_is_batch, params
        n = x1.shape[-2]
        m = x2.shape[-2]

        if self.cont_dims:
            x1_cont = x1[..., self.cont_dims]
            x2_cont = x2[..., self.cont_dims]
            kx = self.cont_kernel(x1_cont, x2_cont).to_dense()
        else:
            kx = torch.ones(*x1.shape[:-2], n, m, dtype=x1.dtype, device=x1.device)

        kh = self.cat_kernel(x1, x2).to_dense() if self.cat_dims else torch.ones_like(kx)
        lam = self.lambda_val.to(dtype=x1.dtype, device=x1.device)
        return (1.0 - lam) * (kh + kx) + lam * kh * kx
