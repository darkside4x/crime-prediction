"""Typed errors raised by the modeling pipeline."""


class ModelingError(Exception):
    """Base class for expected modeling failures."""


class ConfigurationError(ModelingError):
    """The experiment configuration is invalid."""


class DataContractError(ModelingError):
    """Input rows do not satisfy the feature-table contract."""


class OptionalDependencyError(ModelingError):
    """A requested optional integration is not installed."""
