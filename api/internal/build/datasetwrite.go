package build

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"reflect"
	"strings"

	"github.com/Zeus-Development/proxy-index/api/internal/model"
)

// WriteDataset writes d as the bytes the server's Marshal gives (compact JSON,
// HTML-escaped strings, no trailing newline), one top-level field at a time and
// the providers one by one, so the whole encoded dataset is never in memory:
// at most one provider's encoding is; the pages (DatasetPages) are written
// one answer at a time and the lists (DatasetLists) one list's first page at
// a time, from the pages the build keeps. Every Dataset field must be a plain
// `json:"name"` field (no omitempty, no "-"), which is what keeps the output
// equal to encoding the struct whole; anything else is an error.
func WriteDataset(w io.Writer, d *model.Dataset) error {
	bw := bufio.NewWriterSize(w, 64<<10)
	v := reflect.ValueOf(d).Elem()
	t := v.Type()
	lists, isLists := d.Lists.(*DatasetLists)
	pages, isPages := d.Pages.(DatasetPages)
	bw.WriteByte('{') //nolint:errcheck // bufio reports the first error at Flush
	for i := range t.NumField() {
		f := t.Field(i)
		name, opts, _ := strings.Cut(f.Tag.Get("json"), ",")
		if !f.IsExported() || name == "" || name == "-" || opts != "" {
			return fmt.Errorf("dataset field %s has json tag %q: WriteDataset needs plain names", f.Name, f.Tag.Get("json"))
		}
		if i > 0 {
			bw.WriteByte(',') //nolint:errcheck
		}
		key, _ := json.Marshal(name) // a string always encodes
		bw.Write(key)                //nolint:errcheck
		bw.WriteByte(':')            //nolint:errcheck
		var err error
		switch {
		case f.Name == "Providers":
			err = writeProviders(bw, d.Providers)
		case f.Name == "Lists" && isLists && lists != nil:
			err = lists.write(bw)
		case f.Name == "Pages" && isPages && pages != nil:
			err = pages.write(bw)
		default:
			// The field's address, as encoding the whole struct through a
			// pointer does, so pointer-receiver marshalers apply the same way.
			err = writeValue(bw, v.Field(i).Addr().Interface())
		}
		if err != nil {
			return fmt.Errorf("dataset %s: %w", name, err)
		}
	}
	bw.WriteByte('}') //nolint:errcheck
	return bw.Flush()
}

func writeProviders(w *bufio.Writer, providers []*model.Provider) error {
	if providers == nil {
		_, err := w.WriteString("null")
		return err
	}
	w.WriteByte('[') //nolint:errcheck
	for i, p := range providers {
		if i > 0 {
			w.WriteByte(',') //nolint:errcheck
		}
		if err := writeValue(w, p); err != nil {
			return err
		}
	}
	return w.WriteByte(']')
}

func writeValue(w *bufio.Writer, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	_, err = w.Write(b)
	return err
}
