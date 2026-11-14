package build

import (
	"fmt"
	"strconv"
)

// Error answers (TYPED-ERRORS-1): every 4xx and 5xx body is
// {"error": {code, params}}, a code of the error set (codes_errors.go) and
// its parameters, so a site in any language writes its own message.

// The error codes.
const (
	ErrNotFound              = "not_found"
	ErrMethodNotAllowed      = "method_not_allowed"
	ErrUnknownParameter      = "unknown_parameter"
	ErrInvalidParameter      = "invalid_parameter"
	ErrOutOfRange            = "out_of_range"
	ErrTooLong               = "too_long"
	ErrUnknownCursor         = "unknown_cursor"
	ErrConflictingParameters = "conflicting_parameters"
	ErrUnavailable           = "unavailable"
	ErrBusy                  = "busy"
	ErrInternal              = "internal_error"
)

// The formats of invalid_parameter (error_format).
const (
	FormatCountry         = "country"
	FormatEventID         = "event_id"
	FormatPositiveInteger = "positive_integer"
	FormatISOTime         = "iso_time"
	FormatSlug            = "slug"
	FormatKind            = "kind"
	FormatText            = "text"
)

// APIError is an error answer's error: a code and its parameters. A Key's
// APIError answers 400.
type APIError struct {
	Code   string         `json:"code"`
	Params map[string]any `json:"params"`
}

// Error is the code and its parameters, for logs and tests.
func (e APIError) Error() string { return fmt.Sprintf("%s %v", e.Code, e.Params) }

// errParams are an APIError's parameters.
type errParams = map[string]any

// NotFound is a path that names no route, network, country, place or file.
func NotFound(path string) APIError { return APIError{ErrNotFound, errParams{"path": path}} }

// MethodNotAllowed is a request method other than GET or HEAD.
func MethodNotAllowed(method string) APIError {
	return APIError{ErrMethodNotAllowed, errParams{"method": method}}
}

// UnknownParameter is a query parameter the route does not take.
func UnknownParameter(name string) APIError {
	return APIError{ErrUnknownParameter, errParams{"name": name}}
}

// InvalidValue is a parameter value outside allowed (nil: no list given).
func InvalidValue(name, value string, allowed ...string) APIError {
	e := APIError{ErrInvalidParameter, errParams{"name": name, "value": value}}
	if allowed != nil {
		e.Params["allowed"] = allowed
	}
	return e
}

// InvalidFormat is a parameter value not in format (error_format).
func InvalidFormat(name, value, format string) APIError {
	return APIError{ErrInvalidParameter, errParams{"name": name, "value": value, "format": format}}
}

// TooLong is a parameter value longer than max bytes.
func TooLong(name string, max int) APIError {
	return APIError{ErrTooLong, errParams{"name": name, "max": max}}
}

// UnknownCursor is a cursor that names no event.
func UnknownCursor(name, value string) APIError {
	return APIError{ErrUnknownCursor, errParams{"name": name, "value": value}}
}

// ConflictingParameters is parameters that cannot be combined.
func ConflictingParameters(names ...string) APIError {
	return APIError{ErrConflictingParameters, errParams{"names": names}}
}

// Unavailable is a 503 with its reason (unavailable_reason).
func Unavailable(reason string) APIError {
	return APIError{ErrUnavailable, errParams{"reason": reason}}
}

// Busy is a 503: the answer is computed on request, and the server is
// computing as many as it may at once; try again after Retry-After.
func Busy() APIError { return APIError{ErrBusy, errParams{}} }

// Internal is a 500: the answer could not be read or rendered.
func Internal() APIError { return APIError{ErrInternal, errParams{}} }

// IntParam is the integer parameter name's value raw, between lo and hi:
// invalid_parameter (positive_integer) when it is no integer, out_of_range
// when it is outside.
func IntParam(name, raw string, lo, hi int) (int, error) {
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 0, InvalidFormat(name, raw, FormatPositiveInteger)
	}
	if n < lo || n > hi {
		return 0, APIError{ErrOutOfRange, errParams{"name": name, "value": n, "min": lo, "max": hi}}
	}
	return n, nil
}
