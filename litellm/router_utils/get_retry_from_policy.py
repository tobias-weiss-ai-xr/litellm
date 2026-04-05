"""
Get num retries for an exception.

- Account for retry policy by exception type.
"""

from typing import Dict, Optional, Union

from litellm.exceptions import (
    AuthenticationError,
    BadRequestError,
    ContentPolicyViolationError,
    RateLimitError,
    Timeout,
)
from litellm.types.router import RetryPolicy


def get_num_retries_from_retry_policy(
    exception: Exception,
    retry_policy: Optional[Union[RetryPolicy, dict]] = None,
    model_group: Optional[str] = None,
    model_group_retry_policy: Optional[Dict[str, RetryPolicy]] = None,
) -> Optional[int]:
    """
    BadRequestErrorRetries: Optional[int] = None
    AuthenticationErrorRetries: Optional[int] = None
    TimeoutErrorRetries: Optional[int] = None
    RateLimitErrorRetries: Optional[int] = None
    ContentPolicyViolationErrorRetries: Optional[int] = None
    """
    # if we can find the exception then in the retry policy -> return the number of retries

    if model_group_retry_policy is not None and model_group is not None and model_group in model_group_retry_policy:
        retry_policy = model_group_retry_policy.get(model_group)

    if retry_policy is None:
        return None
    if isinstance(retry_policy, dict):
        retry_policy = RetryPolicy(**retry_policy)

    exception_retry_map = {
        AuthenticationError: retry_policy.AuthenticationErrorRetries,
        Timeout: retry_policy.TimeoutErrorRetries,
        RateLimitError: retry_policy.RateLimitErrorRetries,
        ContentPolicyViolationError: retry_policy.ContentPolicyViolationErrorRetries,
        BadRequestError: retry_policy.BadRequestErrorRetries,
    }

    for exc_type, retries in exception_retry_map.items():
        if isinstance(exception, exc_type) and retries is not None:
            return retries


def reset_retry_policy() -> RetryPolicy:
    return RetryPolicy()
