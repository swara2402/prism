from dataclasses import dataclass

@dataclass
class EffectivenessConfig:
    """Configuration for action effectiveness handling.

    * ``default_prior`` – neutral prior for unseen (context, action) pairs.
    * ``ema_alpha`` – smoothing factor used by EMA updates in
      ``investigation.action_effectiveness``.
    """
    default_prior: float = 0.5
    ema_alpha: float = 0.8
