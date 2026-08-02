"""Shared OAuth protocol primitives.

The underscored helpers remain internal so provider flows cannot become a
second public authentication API.
"""

from ._flows import OAuthError, _pkce, _state, _token

__all__ = ["OAuthError"]
