from enum import Enum


class Error(Enum):
    UNSUCCESSFUL_RESPONSE = 1
    UNSUPPORTED_DOMAIN = 2
    DOES_NOT_EXIST = 3
    FAILED_TO_LOCATE = 4
    FORBIDDEN = 5
    TEXT_LINK_FAILURE = 6
    FAILED_TO_EXTRACT = 7
    FAILED_SELF_POST = 8
    DUPLICATE_CONTENT = 9
    FAILED_FILTER = 10
    MULTIPART_FAILURE = 11
    UNKNOWN_ERROR = 12
    CONNECTION_ERROR = 13
    DOWNLOAD_STOPPED = 14
    TEXT_FAILURE = 15
    UNRECOGNIZED_EXTENSION = 16
    RATE_LIMIT_ERROR = 17
    CREDIT_ERROR = 18


class Disposition(Enum):
    PERMANENT = "permanent"  # never retry; policy failure, not a transient condition
    TRANSIENT = "transient"  # retry with backoff
    DEFERRED = "deferred"  # retry after a cooldown (rate-limited)
    INTERRUPTED = (
        "interrupted"  # retry immediately; does not count against the retry budget
    )


# Maps every Error to a retry disposition.
DISPOSITION: dict[Error, Disposition] = {
    # --- permanent: never retry ---
    Error.UNSUPPORTED_DOMAIN: Disposition.PERMANENT,
    Error.DOES_NOT_EXIST: Disposition.PERMANENT,
    Error.FAILED_TO_LOCATE: Disposition.PERMANENT,
    Error.FORBIDDEN: Disposition.PERMANENT,
    Error.FAILED_SELF_POST: Disposition.PERMANENT,
    Error.DUPLICATE_CONTENT: Disposition.PERMANENT,
    Error.FAILED_FILTER: Disposition.PERMANENT,
    Error.UNRECOGNIZED_EXTENSION: Disposition.PERMANENT,
    # --- transient: retry with backoff ---
    Error.UNSUCCESSFUL_RESPONSE: Disposition.TRANSIENT,
    Error.TEXT_LINK_FAILURE: Disposition.TRANSIENT,
    Error.FAILED_TO_EXTRACT: Disposition.TRANSIENT,
    Error.MULTIPART_FAILURE: Disposition.TRANSIENT,
    Error.UNKNOWN_ERROR: Disposition.TRANSIENT,
    Error.CONNECTION_ERROR: Disposition.TRANSIENT,
    Error.TEXT_FAILURE: Disposition.TRANSIENT,
    Error.CREDIT_ERROR: Disposition.TRANSIENT,
    # --- deferred: retry after cooldown ---
    Error.RATE_LIMIT_ERROR: Disposition.DEFERRED,
    # --- interrupted: retry immediately, doesn't count against budget ---
    Error.DOWNLOAD_STOPPED: Disposition.INTERRUPTED,
}

# Errors whose failures should never be retried.
PERMANENT_ERRORS = frozenset(
    error
    for error, disposition in DISPOSITION.items()
    if disposition == Disposition.PERMANENT
)
