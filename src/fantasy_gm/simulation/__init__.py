"""Deterministic, seedable simulation interfaces.

Simulation is the final decision layer; it consumes predictive distributions produced upstream
and must never compensate for a weak projection model. v0.1 ships interfaces plus reference
implementations that sample *given* distributions -- no projection logic lives here.
"""
