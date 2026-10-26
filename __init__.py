"""Hermes plugin entry. Hermes calls register(ctx) after reading plugin.yaml."""

from .taskwatch.plugin import register

__all__ = ["register"]
