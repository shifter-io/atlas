package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"time"

	"github.com/Zeus-Development/proxy-index/api/internal/server"
)

// purgeTimeout bounds `siteapi cdn-purge`'s one request.
const purgeTimeout = 30 * time.Second

// purgeFlags are the CDN purge flags of serve and cdn-purge.
type purgeFlags struct {
	purgeURL, keyFile string
}

func (p *purgeFlags) register(fs *flag.FlagSet) {
	fs.StringVar(&p.purgeURL, "cdn-purge-url", env("SITEAPI_CDN_PURGE_URL", ""),
		"the CDN's purge endpoint for the pull zone; with --cdn-key-file, after each new build the --cdn-tag copies are purged before /v1/version names it")
	fs.StringVar(&p.keyFile, "cdn-key-file", env("SITEAPI_CDN_KEY_FILE", ""),
		"private file (mode 600) holding the CDN's API key, sent in the AccessKey header and never logged or shown")
}

// purger is the configured server.Purger for tag, nil when neither flag is set.
func (p purgeFlags) purger(tag string) (*server.Purger, error) {
	if p.purgeURL == "" && p.keyFile == "" {
		return nil, nil
	}
	if p.purgeURL == "" || p.keyFile == "" {
		return nil, errors.New("--cdn-purge-url and --cdn-key-file go together")
	}
	return server.NewPurger(p.purgeURL, p.keyFile, tag)
}

// cdnPurge purges the CDN's copies carrying --cdn-tag once: after a deploy's
// proxy switch, before the old colour stops.
func cdnPurge(args []string) error {
	fs := flag.NewFlagSet("cdn-purge", flag.ExitOnError)
	var p purgeFlags
	p.register(fs)
	tag := fs.String("cdn-tag", env("SITEAPI_CDN_TAG", server.DefaultCDNTag), "the CDN-Tag whose copies are purged")
	fs.Parse(args) //nolint:errcheck
	if p.purgeURL == "" && p.keyFile == "" {
		return errors.New("--cdn-purge-url and --cdn-key-file are required")
	}
	pg, err := p.purger(*tag)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), purgeTimeout)
	defer cancel()
	if err := pg.Purge(ctx); err != nil {
		return err
	}
	fmt.Println("CDN purged: tag", *tag)
	return nil
}
