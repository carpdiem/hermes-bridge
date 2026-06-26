class BridgeError(RuntimeError):
    """User-facing error from hermes-bridge."""


class ConfigError(BridgeError):
    """Configuration is missing or invalid."""


class CapabilityError(BridgeError):
    """Requested capability is not enabled for the selected agent."""
