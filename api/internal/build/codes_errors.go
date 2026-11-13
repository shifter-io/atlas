package build

// The error answers and /healthz's reasons (TYPED-ERRORS-1).

// The reasons of unavailable, and of /healthz reason.
const (
	ReasonBeforeV4         = "payload_before_v4"
	ReasonCacheNotBuilt    = "cache_not_built"
	ReasonDatasetFile      = "dataset_file_missing"
	ReasonEventsQueryFails = "events_query_failed"
)

func init() {
	name, value := param("name", "string", ""), param("value", "string", "")
	RegisterCodeSet(CodeSet{Name: "error", Source: CodesFromAPI, UsedIn: []string{"* error.code"}, Values: []Code{
		withParams(ErrNotFound, "Nothing is at {path}.", param("path", "path", "")),
		withParams(ErrMethodNotAllowed, "The method {method} is not served: use GET or HEAD.", param("method", "string", "")),
		withParams(ErrUnknownParameter, "The route takes no parameter {name}.", name),
		withParams(ErrInvalidParameter, "{value} is no valid {name}: one of {allowed}, or a value in {format}.", name, value,
			param("allowed", "string_list", ""), param("format", "code:error_format", "")),
		withParams(ErrOutOfRange, "{name} must be between {min} and {max}, not {value}.", name, param("value", "integer", ""),
			param("min", "integer", ""), param("max", "integer", "")),
		withParams(ErrTooLong, "{name} is longer than {max} bytes.", name, param("max", "integer", "")),
		withParams(ErrUnknownCursor, "The cursor {name}={value} names no event.", name, value),
		withParams(ErrConflictingParameters, "The parameters {names} cannot be combined.", param("names", "string_list", "")),
		withParams(ErrUnavailable, "Not served now: {reason}.", param("reason", "code:unavailable_reason", "")),
		withParams(ErrInternal, "The answer could not be read; try again."),
	}})
	RegisterCodeSet(CodeSet{Name: "error_format", Source: CodesFromAPI, UsedIn: []string{"* error.params.format"},
		Values: codes(FormatCountry, "A two-letter country code.", FormatEventID, "An event id: a whole number from 0.",
			FormatPositiveInteger, "A whole number.", FormatISOTime, "An ISO 8601 time or date.",
			FormatSlug, "A network slug: lowercase letters, digits and hyphens.",
			FormatKind, "A kind: lowercase letters, digits and underscores.")})
	RegisterCodeSet(CodeSet{Name: "unavailable_reason", Source: CodesFromAPI, UsedIn: []string{"* error.params.reason", "/healthz reason"},
		Values: codes(ReasonBeforeV4, "The published calculation is older than payload v4: recalculate.",
			ReasonCacheNotBuilt, "The server has not built its answers yet.",
			ReasonDatasetFile, "The dataset file cannot be read.",
			ReasonEventsQueryFails, "The events could not be read from the database.")})
	RegisterCodeSet(CodeSet{Name: "unserved", Source: CodesFromAPI, UsedIn: []string{"/healthz unserved[].code"},
		Values: codes("build_failed", "The page's answer failed to build.", "not_found", "The page answers 404.",
			"no_network_page", "A published product has no network.",
			"not_served", "The page answers another status than 200 (status).")})
}
