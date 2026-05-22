"""Descriptor framework v2 for chemistry-aware categorical representations."""

from .registry import DescriptorRegistry, build_descriptor_feature_spec
from .schema import DescriptorMatrix, DescriptorSpec, DescriptorValue

__all__ = [
    "DescriptorMatrix",
    "DescriptorRegistry",
    "DescriptorSpec",
    "DescriptorValue",
    "build_descriptor_feature_spec",
]

