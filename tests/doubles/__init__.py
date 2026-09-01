"""Test doubles shared across test modules.

Doubles live here rather than inline in the module that uses them so that a
double and the production surface it stands in for cannot drift apart unnoticed
— which is exactly how the loader tests broke.
"""
