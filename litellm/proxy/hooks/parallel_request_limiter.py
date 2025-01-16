import asyncio
import sys
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Tuple, TypedDict, Union

from fastapi import HTTPException
from pydantic import BaseModel

import litellm
from litellm import DualCache, ModelResponse
from litellm._logging import verbose_proxy_logger
from litellm.constants import RATE_LIMIT_ERROR_MESSAGE_FOR_VIRTUAL_KEY
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import _get_parent_otel_span_from_kwargs
from litellm.proxy._types import CurrentItemRateLimit, UserAPIKeyAuth
from litellm.proxy.auth.auth_utils import (
    get_key_model_rpm_limit,
    get_key_model_tpm_limit,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Span as _Span

    from litellm.proxy.utils import InternalUsageCache as _InternalUsageCache

    Span = _Span
    InternalUsageCache = _InternalUsageCache
else:
    Span = Any
    InternalUsageCache = Any


class CacheObject(TypedDict):
    current_global_requests: Optional[dict]
    request_count_api_key: Optional[dict]
    request_count_user_id: Optional[dict]
    request_count_team_id: Optional[dict]
    request_count_end_user_id: Optional[dict]


class _PROXY_MaxParallelRequestsHandler(CustomLogger):
    # Class variables or attributes
    def __init__(self, internal_usage_cache: InternalUsageCache):
        self.internal_usage_cache = internal_usage_cache

    def print_verbose(self, print_statement):
        try:
            verbose_proxy_logger.debug(print_statement)
            if litellm.set_verbose:
                print(print_statement)  # noqa
        except Exception:
            pass

    async def check_key_in_limits(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
        max_parallel_requests: int,
        tpm_limit: int,
        rpm_limit: int,
        current: Optional[dict],
        request_count_api_key: str,
        rate_limit_type: Literal["user", "customer", "team"],
        values_to_update_in_cache: List[Tuple[Any, Any]],
    ):
        # current = await self.internal_usage_cache.async_get_cache(
        #     key=request_count_api_key,
        #     litellm_parent_otel_span=user_api_key_dict.parent_otel_span,
        # )  # {"current_requests": 1, "current_tpm": 1, "current_rpm": 10}
        if current is None:
            if max_parallel_requests == 0 or tpm_limit == 0 or rpm_limit == 0:
                # base case
                return self.raise_rate_limit_error(
                    additional_details=f"Hit limit for {rate_limit_type}. Current limits: max_parallel_requests: {max_parallel_requests}, tpm_limit: {tpm_limit}, rpm_limit: {rpm_limit}"
                )
            new_val = {
                "current_requests": 1,
                "current_tpm": 0,
                "current_rpm": 0,
            }
            values_to_update_in_cache.append((request_count_api_key, new_val))
        elif (
            int(current["current_requests"]) < max_parallel_requests
            and current["current_tpm"] < tpm_limit
            and current["current_rpm"] < rpm_limit
        ):
            # Increase count for this token
            new_val = {
                "current_requests": current["current_requests"] + 1,
                "current_tpm": current["current_tpm"],
                "current_rpm": current["current_rpm"],
            }
            values_to_update_in_cache.append((request_count_api_key, new_val))
        else:
            raise HTTPException(
                status_code=429,
                detail=f"LiteLLM Rate Limit Handler for rate limit type = {rate_limit_type}. Crossed TPM, RPM Limit. current rpm: {current['current_rpm']}, rpm limit: {rpm_limit}, current tpm: {current['current_tpm']}, tpm limit: {tpm_limit}",
                headers={"retry-after": str(self.time_to_next_minute())},
            )

    def time_to_next_minute(self) -> float:
        # Get the current time
        now = datetime.now()

        # Calculate the next minute
        next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)

        # Calculate the difference in seconds
        seconds_to_next_minute = (next_minute - now).total_seconds()

        return seconds_to_next_minute

    def raise_rate_limit_error(
        self, additional_details: Optional[str] = None
    ) -> HTTPException:
        """
        Raise an HTTPException with a 429 status code and a retry-after header
        """
        error_message = "Max parallel request limit reached"
        if additional_details is not None:
            error_message = error_message + " " + additional_details
        raise HTTPException(
            status_code=429,
            detail=f"Max parallel request limit reached {additional_details}",
            headers={"retry-after": str(self.time_to_next_minute())},
        )

    async def get_all_cache_objects(
        self,
        current_global_requests: Optional[str],
        request_count_api_key: Optional[str],
        request_count_user_id: Optional[str],
        request_count_team_id: Optional[str],
        request_count_end_user_id: Optional[str],
        parent_otel_span: Optional[Span] = None,
    ) -> CacheObject:
        keys = [
            current_global_requests,
            request_count_api_key,
            request_count_user_id,
            request_count_team_id,
            request_count_end_user_id,
        ]
        results = await self.internal_usage_cache.async_batch_get_cache(
            keys=keys,
            parent_otel_span=parent_otel_span,
        )

        if results is None:
            return CacheObject(
                current_global_requests=None,
                request_count_api_key=None,
                request_count_user_id=None,
                request_count_team_id=None,
                request_count_end_user_id=None,
            )

        return CacheObject(
            current_global_requests=results[0],
            request_count_api_key=results[1],
            request_count_user_id=results[2],
            request_count_team_id=results[3],
            request_count_end_user_id=results[4],
        )

    async def async_pre_call_hook(  # noqa: PLR0915
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
    ):
        self.print_verbose("Inside Max Parallel Request Pre-Call Hook")
        api_key = user_api_key_dict.api_key
        max_parallel_requests = user_api_key_dict.max_parallel_requests
        if max_parallel_requests is None:
            max_parallel_requests = sys.maxsize
        if data is None:
            data = {}
        global_max_parallel_requests = data.get("metadata", {}).get(
            "global_max_parallel_requests", None
        )
        tpm_limit = getattr(user_api_key_dict, "tpm_limit", sys.maxsize)
        if tpm_limit is None:
            tpm_limit = sys.maxsize
        rpm_limit = getattr(user_api_key_dict, "rpm_limit", sys.maxsize)
        if rpm_limit is None:
            rpm_limit = sys.maxsize

        values_to_update_in_cache: List[Tuple[Any, Any]] = (
            []
        )  # values that need to get updated in cache, will run a batch_set_cache after this function

        # ------------
        # Setup values
        # ------------
        new_val: Optional[dict] = None

        if global_max_parallel_requests is not None:
            # get value from cache
            _key = "global_max_parallel_requests"
            current_global_requests = await self.internal_usage_cache.async_get_cache(
                key=_key,
                local_only=True,
                litellm_parent_otel_span=user_api_key_dict.parent_otel_span,
            )
            # check if below limit
            if current_global_requests is None:
                current_global_requests = 1
            # if above -> raise error
            if current_global_requests >= global_max_parallel_requests:
                return self.raise_rate_limit_error(
                    additional_details=f"Hit Global Limit: Limit={global_max_parallel_requests}, current: {current_global_requests}"
                )
            # if below -> increment
            else:
                await self.internal_usage_cache.async_increment_cache(
                    key=_key,
                    value=1,
                    local_only=True,
                    litellm_parent_otel_span=user_api_key_dict.parent_otel_span,
                )

        current_date = datetime.now().strftime("%Y-%m-%d")
        current_hour = datetime.now().strftime("%H")
        current_minute = datetime.now().strftime("%M")
        precise_minute = f"{current_date}-{current_hour}-{current_minute}"

        cache_objects: CacheObject = await self.get_all_cache_objects(
            current_global_requests=(
                "global_max_parallel_requests"
                if global_max_parallel_requests is not None
                else None
            ),
            request_count_api_key=(
                f"{api_key}::{precise_minute}::request_count"
                if api_key is not None
                else None
            ),
            request_count_user_id=(
                f"{user_api_key_dict.user_id}::{precise_minute}::request_count"
                if user_api_key_dict.user_id is not None
                else None
            ),
            request_count_team_id=(
                f"{user_api_key_dict.team_id}::{precise_minute}::request_count"
                if user_api_key_dict.team_id is not None
                else None
            ),
            request_count_end_user_id=(
                f"{user_api_key_dict.end_user_id}::{precise_minute}::request_count"
                if user_api_key_dict.end_user_id is not None
                else None
            ),
            parent_otel_span=user_api_key_dict.parent_otel_span,
        )
        if api_key is not None:
            request_count_api_key = f"{api_key}::{precise_minute}::request_count"

            # CHECK IF REQUEST ALLOWED for key

            current = cache_objects["request_count_api_key"]
            self.print_verbose(f"current: {current}")
            if (
                max_parallel_requests == sys.maxsize
                and tpm_limit == sys.maxsize
                and rpm_limit == sys.maxsize
            ):
                pass
            elif max_parallel_requests == 0 or tpm_limit == 0 or rpm_limit == 0:
                return self.raise_rate_limit_error(
                    additional_details=f"Hit limit for {RATE_LIMIT_ERROR_MESSAGE_FOR_VIRTUAL_KEY}: {api_key}. max_parallel_requests: {max_parallel_requests}, tpm_limit: {tpm_limit}, rpm_limit: {rpm_limit}"
                )
            elif current is None:
                new_val = {
                    "current_requests": 1,
                    "current_tpm": 0,
                    "current_rpm": 0,
                }
                values_to_update_in_cache.append((request_count_api_key, new_val))
            elif (
                int(current["current_requests"]) < max_parallel_requests
                and current["current_tpm"] < tpm_limit
                and current["current_rpm"] < rpm_limit
            ):
                # Increase count for this token
                new_val = {
                    "current_requests": current["current_requests"] + 1,
                    "current_tpm": current["current_tpm"],
                    "current_rpm": current["current_rpm"],
                }
                values_to_update_in_cache.append((request_count_api_key, new_val))
            else:
                return self.raise_rate_limit_error(
                    additional_details=f"Hit limit for {RATE_LIMIT_ERROR_MESSAGE_FOR_VIRTUAL_KEY}: {api_key}. tpm_limit: {tpm_limit}, current_tpm {current['current_tpm']} , rpm_limit: {rpm_limit} current rpm {current['current_rpm']} "
                )

        # Check if request under RPM/TPM per model for a given API Key
        if (
            get_key_model_tpm_limit(user_api_key_dict) is not None
            or get_key_model_rpm_limit(user_api_key_dict) is not None
        ):
            _model = data.get("model", None)
            request_count_api_key = (
                f"{api_key}::{_model}::{precise_minute}::request_count"
            )

            current = await self.internal_usage_cache.async_get_cache(
                key=request_count_api_key,
                litellm_parent_otel_span=user_api_key_dict.parent_otel_span,
            )  # {"current_requests": 1, "current_tpm": 1, "current_rpm": 10}

            tpm_limit_for_model = None
            rpm_limit_for_model = None

            _tpm_limit_for_key_model = get_key_model_tpm_limit(user_api_key_dict)
            _rpm_limit_for_key_model = get_key_model_rpm_limit(user_api_key_dict)
            if _model is not None:

                if _tpm_limit_for_key_model:
                    tpm_limit_for_model = _tpm_limit_for_key_model.get(_model)

                if _rpm_limit_for_key_model:
                    rpm_limit_for_model = _rpm_limit_for_key_model.get(_model)

            if current is None:
                new_val = {
                    "current_requests": 1,
                    "current_tpm": 0,
                    "current_rpm": 0,
                }
                values_to_update_in_cache.append((request_count_api_key, new_val))
            elif tpm_limit_for_model is not None or rpm_limit_for_model is not None:
                # Increase count for this token
                new_val = {
                    "current_requests": current["current_requests"] + 1,
                    "current_tpm": current["current_tpm"],
                    "current_rpm": current["current_rpm"],
                }
                if (
                    tpm_limit_for_model is not None
                    and current["current_tpm"] >= tpm_limit_for_model
                ):
                    return self.raise_rate_limit_error(
                        additional_details=f"Hit TPM limit for model: {_model} on {RATE_LIMIT_ERROR_MESSAGE_FOR_VIRTUAL_KEY}: {api_key}. tpm_limit: {tpm_limit_for_model}, current_tpm {current['current_tpm']} "
                    )
                elif (
                    rpm_limit_for_model is not None
                    and current["current_rpm"] >= rpm_limit_for_model
                ):
                    return self.raise_rate_limit_error(
                        additional_details=f"Hit RPM limit for model: {_model} on {RATE_LIMIT_ERROR_MESSAGE_FOR_VIRTUAL_KEY}: {api_key}. rpm_limit: {rpm_limit_for_model}, current_rpm {current['current_rpm']} "
                    )
                else:
                    values_to_update_in_cache.append((request_count_api_key, new_val))

            _remaining_tokens = None
            _remaining_requests = None
            # Add remaining tokens, requests to metadata
            if new_val:
                if tpm_limit_for_model is not None:
                    _remaining_tokens = tpm_limit_for_model - new_val["current_tpm"]
                if rpm_limit_for_model is not None:
                    _remaining_requests = rpm_limit_for_model - new_val["current_rpm"]

            _remaining_limits_data = {
                f"litellm-key-remaining-tokens-{_model}": _remaining_tokens,
                f"litellm-key-remaining-requests-{_model}": _remaining_requests,
            }

            if "metadata" not in data:
                data["metadata"] = {}
            data["metadata"].update(_remaining_limits_data)

        # check if REQUEST ALLOWED for user_id
        user_id = user_api_key_dict.user_id
        if user_id is not None:
            user_tpm_limit = user_api_key_dict.user_tpm_limit
            user_rpm_limit = user_api_key_dict.user_rpm_limit
            if user_tpm_limit is None:
                user_tpm_limit = sys.maxsize
            if user_rpm_limit is None:
                user_rpm_limit = sys.maxsize

            request_count_api_key = f"{user_id}::{precise_minute}::request_count"
            # print(f"Checking if {request_count_api_key} is allowed to make request for minute {precise_minute}")
            await self.check_key_in_limits(
                user_api_key_dict=user_api_key_dict,
                cache=cache,
                data=data,
                call_type=call_type,
                max_parallel_requests=sys.maxsize,  # TODO: Support max parallel requests for a user
                current=cache_objects["request_count_user_id"],
                request_count_api_key=request_count_api_key,
                tpm_limit=user_tpm_limit,
                rpm_limit=user_rpm_limit,
                rate_limit_type="user",
                values_to_update_in_cache=values_to_update_in_cache,
            )

        # TEAM RATE LIMITS
        ## get team tpm/rpm limits
        team_id = user_api_key_dict.team_id
        if team_id is not None:
            team_tpm_limit = user_api_key_dict.team_tpm_limit
            team_rpm_limit = user_api_key_dict.team_rpm_limit

            if team_tpm_limit is None:
                team_tpm_limit = sys.maxsize
            if team_rpm_limit is None:
                team_rpm_limit = sys.maxsize

            request_count_api_key = f"{team_id}::{precise_minute}::request_count"
            # print(f"Checking if {request_count_api_key} is allowed to make request for minute {precise_minute}")
            await self.check_key_in_limits(
                user_api_key_dict=user_api_key_dict,
                cache=cache,
                data=data,
                call_type=call_type,
                max_parallel_requests=sys.maxsize,  # TODO: Support max parallel requests for a team
                current=cache_objects["request_count_team_id"],
                request_count_api_key=request_count_api_key,
                tpm_limit=team_tpm_limit,
                rpm_limit=team_rpm_limit,
                rate_limit_type="team",
                values_to_update_in_cache=values_to_update_in_cache,
            )

        # End-User Rate Limits
        # Only enforce if user passed `user` to /chat, /completions, /embeddings
        if user_api_key_dict.end_user_id:
            end_user_tpm_limit = getattr(
                user_api_key_dict, "end_user_tpm_limit", sys.maxsize
            )
            end_user_rpm_limit = getattr(
                user_api_key_dict, "end_user_rpm_limit", sys.maxsize
            )

            if end_user_tpm_limit is None:
                end_user_tpm_limit = sys.maxsize
            if end_user_rpm_limit is None:
                end_user_rpm_limit = sys.maxsize

            # now do the same tpm/rpm checks
            request_count_api_key = (
                f"{user_api_key_dict.end_user_id}::{precise_minute}::request_count"
            )

            # print(f"Checking if {request_count_api_key} is allowed to make request for minute {precise_minute}")
            await self.check_key_in_limits(
                user_api_key_dict=user_api_key_dict,
                cache=cache,
                data=data,
                call_type=call_type,
                max_parallel_requests=sys.maxsize,  # TODO: Support max parallel requests for an End-User
                request_count_api_key=request_count_api_key,
                current=cache_objects["request_count_end_user_id"],
                tpm_limit=end_user_tpm_limit,
                rpm_limit=end_user_rpm_limit,
                rate_limit_type="customer",
                values_to_update_in_cache=values_to_update_in_cache,
            )

        asyncio.create_task(
            self.internal_usage_cache.async_batch_set_cache(
                cache_list=values_to_update_in_cache,
                ttl=60,
                litellm_parent_otel_span=user_api_key_dict.parent_otel_span,
            )  # don't block execution for cache updates
        )

        return

    async def async_log_success_event(  # noqa: PLR0915
        self, kwargs, response_obj, start_time, end_time
    ):
        from litellm.proxy.common_utils.callback_utils import (
            get_model_group_from_litellm_kwargs,
        )

        litellm_parent_otel_span: Union[Span, None] = _get_parent_otel_span_from_kwargs(
            kwargs=kwargs
        )
        try:
            self.print_verbose("INSIDE parallel request limiter ASYNC SUCCESS LOGGING")

            global_max_parallel_requests = kwargs["litellm_params"]["metadata"].get(
                "global_max_parallel_requests", None
            )
            user_api_key = kwargs["litellm_params"]["metadata"]["user_api_key"]
            user_api_key_user_id = kwargs["litellm_params"]["metadata"].get(
                "user_api_key_user_id", None
            )
            user_api_key_team_id = kwargs["litellm_params"]["metadata"].get(
                "user_api_key_team_id", None
            )
            user_api_key_model_max_budget = kwargs["litellm_params"]["metadata"].get(
                "user_api_key_model_max_budget", None
            )
            user_api_key_end_user_id = kwargs.get("user")

            user_api_key_metadata = (
                kwargs["litellm_params"]["metadata"].get("user_api_key_metadata", {})
                or {}
            )

            # ------------
            # Setup values
            # ------------

            if global_max_parallel_requests is not None:
                # get value from cache
                _key = "global_max_parallel_requests"
                # decrement
                await self.internal_usage_cache.async_increment_cache(
                    key=_key,
                    value=-1,
                    local_only=True,
                    litellm_parent_otel_span=litellm_parent_otel_span,
                )

            current_date = datetime.now().strftime("%Y-%m-%d")
            current_hour = datetime.now().strftime("%H")
            current_minute = datetime.now().strftime("%M")
            precise_minute = f"{current_date}-{current_hour}-{current_minute}"

            total_tokens = 0

            if isinstance(response_obj, ModelResponse):
                total_tokens = response_obj.usage.total_tokens  # type: ignore

            # ------------
            # Update usage - API Key
            # ------------

            values_to_update_in_cache = []

            if user_api_key is not None:
                request_count_api_key = (
                    f"{user_api_key}::{precise_minute}::request_count"
                )

                current = await self.internal_usage_cache.async_get_cache(
                    key=request_count_api_key,
                    litellm_parent_otel_span=litellm_parent_otel_span,
                ) or {
                    "current_requests": 1,
                    "current_tpm": 0,
                    "current_rpm": 0,
                }

                new_val = {
                    "current_requests": max(current["current_requests"] - 1, 0),
                    "current_tpm": current["current_tpm"] + total_tokens,
                    "current_rpm": current["current_rpm"] + 1,
                }

                self.print_verbose(
                    f"updated_value in success call: {new_val}, precise_minute: {precise_minute}"
                )
                values_to_update_in_cache.append((request_count_api_key, new_val))

            # ------------
            # Update usage - model group + API Key
            # ------------
            model_group = get_model_group_from_litellm_kwargs(kwargs)
            if (
                user_api_key is not None
                and model_group is not None
                and (
                    "model_rpm_limit" in user_api_key_metadata
                    or "model_tpm_limit" in user_api_key_metadata
                    or user_api_key_model_max_budget is not None
                )
            ):
                request_count_api_key = (
                    f"{user_api_key}::{model_group}::{precise_minute}::request_count"
                )

                current = await self.internal_usage_cache.async_get_cache(
                    key=request_count_api_key,
                    litellm_parent_otel_span=litellm_parent_otel_span,
                ) or {
                    "current_requests": 1,
                    "current_tpm": 0,
                    "current_rpm": 0,
                }

                new_val = {
                    "current_requests": max(current["current_requests"] - 1, 0),
                    "current_tpm": current["current_tpm"] + total_tokens,
                    "current_rpm": current["current_rpm"] + 1,
                }

                self.print_verbose(
                    f"updated_value in success call: {new_val}, precise_minute: {precise_minute}"
                )
                values_to_update_in_cache.append((request_count_api_key, new_val))

            # ------------
            # Update usage - User
            # ------------
            if user_api_key_user_id is not None:
                total_tokens = 0

                if isinstance(response_obj, ModelResponse):
                    total_tokens = response_obj.usage.total_tokens  # type: ignore

                request_count_api_key = (
                    f"{user_api_key_user_id}::{precise_minute}::request_count"
                )

                current = await self.internal_usage_cache.async_get_cache(
                    key=request_count_api_key,
                    litellm_parent_otel_span=litellm_parent_otel_span,
                ) or {
                    "current_requests": 1,
                    "current_tpm": total_tokens,
                    "current_rpm": 1,
                }

                new_val = {
                    "current_requests": max(current["current_requests"] - 1, 0),
                    "current_tpm": current["current_tpm"] + total_tokens,
                    "current_rpm": current["current_rpm"] + 1,
                }

                self.print_verbose(
                    f"updated_value in success call: {new_val}, precise_minute: {precise_minute}"
                )
                values_to_update_in_cache.append((request_count_api_key, new_val))

            # ------------
            # Update usage - Team
            # ------------
            if user_api_key_team_id is not None:
                total_tokens = 0

                if isinstance(response_obj, ModelResponse):
                    total_tokens = response_obj.usage.total_tokens  # type: ignore

                request_count_api_key = (
                    f"{user_api_key_team_id}::{precise_minute}::request_count"
                )

                current = await self.internal_usage_cache.async_get_cache(
                    key=request_count_api_key,
                    litellm_parent_otel_span=litellm_parent_otel_span,
                ) or {
                    "current_requests": 1,
                    "current_tpm": total_tokens,
                    "current_rpm": 1,
                }

                new_val = {
                    "current_requests": max(current["current_requests"] - 1, 0),
                    "current_tpm": current["current_tpm"] + total_tokens,
                    "current_rpm": current["current_rpm"] + 1,
                }

                self.print_verbose(
                    f"updated_value in success call: {new_val}, precise_minute: {precise_minute}"
                )
                values_to_update_in_cache.append((request_count_api_key, new_val))

            # ------------
            # Update usage - End User
            # ------------
            if user_api_key_end_user_id is not None:
                total_tokens = 0

                if isinstance(response_obj, ModelResponse):
                    total_tokens = response_obj.usage.total_tokens  # type: ignore

                request_count_api_key = (
                    f"{user_api_key_end_user_id}::{precise_minute}::request_count"
                )

                current = await self.internal_usage_cache.async_get_cache(
                    key=request_count_api_key,
                    litellm_parent_otel_span=litellm_parent_otel_span,
                ) or {
                    "current_requests": 1,
                    "current_tpm": total_tokens,
                    "current_rpm": 1,
                }

                new_val = {
                    "current_requests": max(current["current_requests"] - 1, 0),
                    "current_tpm": current["current_tpm"] + total_tokens,
                    "current_rpm": current["current_rpm"] + 1,
                }

                self.print_verbose(
                    f"updated_value in success call: {new_val}, precise_minute: {precise_minute}"
                )
                values_to_update_in_cache.append((request_count_api_key, new_val))

            await self.internal_usage_cache.async_batch_set_cache(
                cache_list=values_to_update_in_cache,
                ttl=60,
                litellm_parent_otel_span=litellm_parent_otel_span,
            )
        except Exception as e:
            self.print_verbose(e)  # noqa

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            self.print_verbose("Inside Max Parallel Request Failure Hook")
            litellm_parent_otel_span: Union[Span, None] = (
                _get_parent_otel_span_from_kwargs(kwargs=kwargs)
            )
            _metadata = kwargs["litellm_params"].get("metadata", {}) or {}
            global_max_parallel_requests = _metadata.get(
                "global_max_parallel_requests", None
            )
            user_api_key = _metadata.get("user_api_key", None)
            self.print_verbose(f"user_api_key: {user_api_key}")
            if user_api_key is None:
                return

            ## decrement call count if call failed
            if "Max parallel request limit reached" in str(kwargs["exception"]):
                pass  # ignore failed calls due to max limit being reached
            else:
                # ------------
                # Setup values
                # ------------

                if global_max_parallel_requests is not None:
                    # get value from cache
                    _key = "global_max_parallel_requests"
                    (
                        await self.internal_usage_cache.async_get_cache(
                            key=_key,
                            local_only=True,
                            litellm_parent_otel_span=litellm_parent_otel_span,
                        )
                    )
                    # decrement
                    await self.internal_usage_cache.async_increment_cache(
                        key=_key,
                        value=-1,
                        local_only=True,
                        litellm_parent_otel_span=litellm_parent_otel_span,
                    )

                current_date = datetime.now().strftime("%Y-%m-%d")
                current_hour = datetime.now().strftime("%H")
                current_minute = datetime.now().strftime("%M")
                precise_minute = f"{current_date}-{current_hour}-{current_minute}"

                request_count_api_key = (
                    f"{user_api_key}::{precise_minute}::request_count"
                )

                # ------------
                # Update usage
                # ------------
                current = await self.internal_usage_cache.async_get_cache(
                    key=request_count_api_key,
                    litellm_parent_otel_span=litellm_parent_otel_span,
                ) or {
                    "current_requests": 1,
                    "current_tpm": 0,
                    "current_rpm": 0,
                }

                new_val = {
                    "current_requests": max(current["current_requests"] - 1, 0),
                    "current_tpm": current["current_tpm"],
                    "current_rpm": current["current_rpm"],
                }

                self.print_verbose(f"updated_value in failure call: {new_val}")
                await self.internal_usage_cache.async_set_cache(
                    request_count_api_key,
                    new_val,
                    ttl=60,
                    litellm_parent_otel_span=litellm_parent_otel_span,
                )  # save in cache for up to 1 min.
        except Exception as e:
            verbose_proxy_logger.exception(
                "Inside Parallel Request Limiter: An exception occurred - {}".format(
                    str(e)
                )
            )

    async def get_internal_user_object(
        self,
        user_id: str,
        user_api_key_dict: UserAPIKeyAuth,
    ) -> Optional[dict]:
        """
        Helper to get the 'Internal User Object'

        It uses the `get_user_object` function from `litellm.proxy.auth.auth_checks`

        We need this because the UserApiKeyAuth object does not contain the rpm/tpm limits for a User AND there could be a perf impact by additionally reading the UserTable.
        """
        from litellm._logging import verbose_proxy_logger
        from litellm.proxy.auth.auth_checks import get_user_object
        from litellm.proxy.proxy_server import prisma_client

        try:
            _user_id_rate_limits = await get_user_object(
                user_id=user_id,
                prisma_client=prisma_client,
                user_api_key_cache=self.internal_usage_cache.dual_cache,
                user_id_upsert=False,
                parent_otel_span=user_api_key_dict.parent_otel_span,
                proxy_logging_obj=None,
            )

            if _user_id_rate_limits is None:
                return None

            return _user_id_rate_limits.model_dump()
        except Exception as e:
            verbose_proxy_logger.debug(
                "Parallel Request Limiter: Error getting user object", str(e)
            )
            return None

    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: UserAPIKeyAuth, response
    ):
        """
        Retrieve the key's remaining rate limits.
        """
        api_key = user_api_key_dict.api_key
        current_date = datetime.now().strftime("%Y-%m-%d")
        current_hour = datetime.now().strftime("%H")
        current_minute = datetime.now().strftime("%M")
        precise_minute = f"{current_date}-{current_hour}-{current_minute}"
        request_count_api_key = f"{api_key}::{precise_minute}::request_count"
        current: Optional[CurrentItemRateLimit] = (
            await self.internal_usage_cache.async_get_cache(
                key=request_count_api_key,
                litellm_parent_otel_span=user_api_key_dict.parent_otel_span,
            )
        )

        key_remaining_rpm_limit: Optional[int] = None
        key_rpm_limit: Optional[int] = None
        key_remaining_tpm_limit: Optional[int] = None
        key_tpm_limit: Optional[int] = None
        if current is not None:
            if user_api_key_dict.rpm_limit is not None:
                key_remaining_rpm_limit = (
                    user_api_key_dict.rpm_limit - current["current_rpm"]
                )
                key_rpm_limit = user_api_key_dict.rpm_limit
            if user_api_key_dict.tpm_limit is not None:
                key_remaining_tpm_limit = (
                    user_api_key_dict.tpm_limit - current["current_tpm"]
                )
                key_tpm_limit = user_api_key_dict.tpm_limit

        if hasattr(response, "_hidden_params"):
            _hidden_params = getattr(response, "_hidden_params")
        else:
            _hidden_params = None
        if _hidden_params is not None and (
            isinstance(_hidden_params, BaseModel) or isinstance(_hidden_params, dict)
        ):
            if isinstance(_hidden_params, BaseModel):
                _hidden_params = _hidden_params.model_dump()

            _additional_headers = _hidden_params.get("additional_headers", {}) or {}

            if key_remaining_rpm_limit is not None:
                _additional_headers["x-ratelimit-remaining-requests"] = (
                    key_remaining_rpm_limit
                )
            if key_rpm_limit is not None:
                _additional_headers["x-ratelimit-limit-requests"] = key_rpm_limit
            if key_remaining_tpm_limit is not None:
                _additional_headers["x-ratelimit-remaining-tokens"] = (
                    key_remaining_tpm_limit
                )
            if key_tpm_limit is not None:
                _additional_headers["x-ratelimit-limit-tokens"] = key_tpm_limit

            setattr(
                response,
                "_hidden_params",
                {**_hidden_params, "additional_headers": _additional_headers},
            )

            return await super().async_post_call_success_hook(
                data, user_api_key_dict, response
            )
