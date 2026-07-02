"""
Tool layer (Tier 2 foundation).

A tool is a self-contained capability the model can call. The registry
exposes their schemas to the provider and dispatches calls back to them.
The conversation core never changes when a tool is added — you write one
tool and register it.
"""
