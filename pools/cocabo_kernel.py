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
from gpytorch.constraints import Positive


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


class WeightedIndicatorKernel(gpytorch.kernels.Kernel):
    """Learnable weighted overlap kernel over categorical dimensions."""

    is_stationary = False

    def __init__(self, cat_dims: list[int], init_weights: list[float] | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.cat_dims = list(cat_dims)
        self._weight_constraint = Positive()
        if not self.cat_dims:
            self.register_buffer("raw_weights", torch.zeros(0))
            return
        if init_weights is None or len(init_weights) != len(self.cat_dims):
            init_tensor = torch.ones(len(self.cat_dims), dtype=torch.get_default_dtype())
        else:
            init_tensor = torch.as_tensor(init_weights, dtype=torch.get_default_dtype()).clamp_min(1e-6)
        self.register_parameter(
            "raw_weights",
            torch.nn.Parameter(init_tensor.log()),
        )
        self.register_constraint("raw_weights", self._weight_constraint)

    @property
    def weights(self) -> torch.Tensor:
        if not self.cat_dims:
            return torch.zeros(0, dtype=torch.get_default_dtype(), device=self.raw_weights.device)
        positive = self._weight_constraint.transform(self.raw_weights)
        return positive / positive.sum().clamp_min(1e-12)

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
        weights = self.weights.to(dtype=x1.dtype, device=x1.device)
        return (matches * weights.view(*([1] * (matches.ndim - 1)), -1)).sum(dim=-1)


class ExpHammingKernel(gpytorch.kernels.Kernel):
    """Exponentiated weighted Hamming-distance kernel over categorical dimensions."""

    is_stationary = False

    def __init__(
        self,
        cat_dims: list[int],
        init_weights: list[float] | None = None,
        init_gamma: float = 1.0,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.cat_dims = list(cat_dims)
        self._weight_constraint = Positive()
        self._gamma_constraint = Positive()
        if not self.cat_dims:
            self.register_buffer("raw_weights", torch.zeros(0))
        else:
            if init_weights is None or len(init_weights) != len(self.cat_dims):
                init_tensor = torch.ones(len(self.cat_dims), dtype=torch.get_default_dtype())
            else:
                init_tensor = torch.as_tensor(init_weights, dtype=torch.get_default_dtype()).clamp_min(1e-6)
            self.register_parameter(
                "raw_weights",
                torch.nn.Parameter(init_tensor.log()),
            )
            self.register_constraint("raw_weights", self._weight_constraint)
        gamma_tensor = torch.as_tensor(float(max(init_gamma, 1e-6)), dtype=torch.get_default_dtype())
        self.register_parameter("raw_gamma", torch.nn.Parameter(gamma_tensor.log().reshape(1)))
        self.register_constraint("raw_gamma", self._gamma_constraint)

    @property
    def weights(self) -> torch.Tensor:
        if not self.cat_dims:
            return torch.zeros(0, dtype=torch.get_default_dtype(), device=self.raw_gamma.device)
        positive = self._weight_constraint.transform(self.raw_weights)
        return positive / positive.sum().clamp_min(1e-12)

    @property
    def gamma(self) -> torch.Tensor:
        return self._gamma_constraint.transform(self.raw_gamma)

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
        mismatches = (x1_cat.unsqueeze(-2) != x2_cat.unsqueeze(-3)).to(dtype=x1.dtype)
        weights = self.weights.to(dtype=x1.dtype, device=x1.device)
        distance = (mismatches * weights.view(*([1] * (mismatches.ndim - 1)), -1)).sum(dim=-1)
        gamma = self.gamma.to(dtype=x1.dtype, device=x1.device)
        return torch.exp(-gamma * distance)


class LatentCategoricalKernel(gpytorch.kernels.Kernel):
    """Learnable latent-embedding kernel over categorical variables."""

    is_stationary = False

    def __init__(
        self,
        cat_dims: list[int],
        cat_specs: list[dict[str, Any]],
        latent_dim: int = 2,
        init_lengthscale: float = 1.0,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.cat_dims = list(cat_dims)
        self.cat_specs = [dict(spec) for spec in cat_specs]
        self.latent_dim = max(int(latent_dim), 1)
        self._lengthscale_constraint = Positive()
        self.register_parameter(
            "raw_lengthscale",
            torch.nn.Parameter(
                torch.as_tensor(float(max(init_lengthscale, 1e-6)), dtype=torch.get_default_dtype()).log().reshape(1)
            ),
        )
        self.register_constraint("raw_lengthscale", self._lengthscale_constraint)
        self.embeddings = torch.nn.ParameterList()
        for spec in self.cat_specs:
            n_categories = max(int(spec.get("n_categories", 1)), 1)
            init = 0.01 * torch.randn(n_categories, self.latent_dim, dtype=torch.get_default_dtype())
            self.embeddings.append(torch.nn.Parameter(init))

    @property
    def lengthscale(self) -> torch.Tensor:
        return self._lengthscale_constraint.transform(self.raw_lengthscale)

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
        if len(self.cat_specs) != len(self.cat_dims):
            raise RuntimeError("LatentCategoricalKernel requires cat_specs aligned with cat_dims.")

        sims = []
        lengthscale = self.lengthscale.to(dtype=x1.dtype, device=x1.device).clamp_min(1e-6)
        for spec, dim, embedding_table in zip(self.cat_specs, self.cat_dims, self.embeddings):
            n_categories = max(int(spec.get("n_categories", embedding_table.shape[0])), 1)
            idx1 = torch.round(x1[..., dim]).long().clamp_min(0).clamp_max(n_categories - 1)
            idx2 = torch.round(x2[..., dim]).long().clamp_min(0).clamp_max(n_categories - 1)
            table = embedding_table.to(dtype=x1.dtype, device=x1.device)
            emb1 = table[idx1]
            emb2 = table[idx2]
            sqdist = ((emb1.unsqueeze(-2) - emb2.unsqueeze(-3)) ** 2).sum(dim=-1)
            sims.append(torch.exp(-0.5 * sqdist / (lengthscale**2)))
        return torch.stack(sims, dim=0).mean(dim=0)


class CoCaBOMixedKernel(gpytorch.kernels.Kernel):
    """CoCaBO kernel combining categorical indicator and continuous kernels."""

    is_stationary = False

    def __init__(
        self,
        *,
        cont_dims: list[int],
        cat_dims: list[int],
        base_cont_kernel: gpytorch.kernels.Kernel,
        cat_kernel_type: str = "indicator",
        cat_kernel_params: dict[str, Any] | None = None,
        cat_specs: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.cont_dims = list(cont_dims)
        self.cat_dims = list(cat_dims)
        self.cont_kernel = base_cont_kernel
        self.cat_kernel_type = str(cat_kernel_type or "indicator").strip().lower()
        self.cat_kernel_params = dict(cat_kernel_params or {})
        self.cat_specs = [dict(spec) for spec in (cat_specs or [])]
        self.cat_kernel = self._build_cat_kernel()
        self.register_parameter("raw_lambda", torch.nn.Parameter(torch.zeros(1)))

    def _build_cat_kernel(self) -> gpytorch.kernels.Kernel:
        if self.cat_kernel_type in {"indicator", "overlap"}:
            return IndicatorKernel(self.cat_dims)
        if self.cat_kernel_type in {"weighted_indicator", "weighted_overlap"}:
            return WeightedIndicatorKernel(
                self.cat_dims,
                init_weights=self.cat_kernel_params.get("weights"),
            )
        if self.cat_kernel_type in {"exp_hamming", "product_hamming", "exponential_hamming"}:
            return ExpHammingKernel(
                self.cat_dims,
                init_weights=self.cat_kernel_params.get("weights"),
                init_gamma=float(self.cat_kernel_params.get("gamma", 1.0)),
            )
        if self.cat_kernel_type in {"latent", "latent_embedding", "latent_rbf"}:
            return LatentCategoricalKernel(
                self.cat_dims,
                self.cat_specs,
                latent_dim=int(self.cat_kernel_params.get("latent_dim", 2)),
                init_lengthscale=float(self.cat_kernel_params.get("lengthscale", 1.0)),
            )
        raise ValueError(f"Unsupported categorical kernel type: {self.cat_kernel_type}")

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
