package build_test

import (
	"context"
	"os"
	"testing"

	"github.com/shifter-io/atlas/api/internal/testdb"
)

// The brand names catalog-networks.sql gives the 18 measured networks, by
// framework provider id.
var networkNames = map[string]string{
	"byteful": "Byteful", "dataimpulse": "DataImpulse", "decodo": "Decodo", "evomi": "Evomi", "fleetproxy": "FleetProxy",
	"geonode": "Geonode", "infatica": "Infatica", "iproyal": "IPRoyal", "massive": "Massive", "nodemaven": "NodeMaven",
	"oxylabs": "Oxylabs", "proxy-cheap": "Proxy-Cheap", "proxy-seller": "Proxy-Seller", "proxyempire": "ProxyEmpire",
	"rayobyte": "Rayobyte", "shifter": "Shifter", "soax": "SOAX", "webshare": "Webshare",
}

// The networks catalog names, describes and links all 18 networks, and is an
// upsert: it runs twice, over a price script's minimal row, over a row linked
// under another slug and over a demo row, and keeps an existing product link.
func TestNetworksCatalog(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	for id := range networkNames {
		db.Exec(t, "INSERT INTO providers VALUES ($1) ON CONFLICT DO NOTHING", id)
	}
	db.Exec(t, `INSERT INTO site.providers(slug,name,framework_provider) VALUES ('iproyal','Iproyal','iproyal')`)
	db.Exec(t, `INSERT INTO site.providers(slug,name,framework_provider,kinds) VALUES ('shifter-io','Shifter','shifter','{residential,isp}')`)
	db.Exec(t, `INSERT INTO site.providers(slug,name,is_demo) VALUES ('soax','Soax demo',true)`)
	db.Exec(t, `INSERT INTO site.provider_products(provider_slug,product_kind,url) VALUES ('iproyal','residential_rotating','https://iproyal.com/?ref=index')`)
	sql, err := os.ReadFile(repoFile(t, "api", "scripts", "catalog-networks.sql"))
	if err != nil {
		t.Fatal(err)
	}
	db.Exec(t, string(sql))
	db.Exec(t, string(sql))
	c := db.Conn(t)
	var bad, products int
	if err := c.QueryRow(context.Background(), `SELECT
	  (SELECT count(*) FROM site.providers WHERE framework_provider IS NOT NULL AND (is_demo OR tagline IS NULL OR website IS NULL
	     OR primary_kind <> 'residential' OR measured_product <> 'residential_rotating' OR NOT kinds @> '{residential}')),
	  (SELECT count(*) FROM site.provider_products pp JOIN site.providers s ON s.slug = pp.provider_slug
	     WHERE s.framework_provider IS NOT NULL AND pp.product_kind = 'residential_rotating' AND pp.measured
	       AND pp.framework_product = 'residential' AND pp.url LIKE 'https://%' AND NOT pp.is_demo)`).Scan(&bad, &products); err != nil {
		t.Fatal(err)
	}
	if bad != 0 || products != 18 {
		t.Fatalf("%d incomplete provider rows, %d linked residential products", bad, products)
	}
	var url string
	if err := c.QueryRow(context.Background(), `SELECT url FROM site.provider_products WHERE provider_slug = 'iproyal'`).Scan(&url); err != nil ||
		url != "https://iproyal.com/?ref=index" {
		t.Fatalf("custom link %q, %v", url, err)
	}
	slugs := map[string]string{}
	for id := range networkNames {
		slugs[id] = id
	}
	slugs["shifter"] = "shifter-io"
	res := buildFrom(t, db)
	for id, name := range networkNames {
		d := res.Details[slugs[id]]
		if d == nil {
			t.Errorf("%s: no network %s", id, slugs[id])
			continue
		}
		p := d.Provider
		if p.Name != name || p.Framework == nil || p.Framework.Provider != id {
			t.Errorf("%s: name %q, framework %+v", id, p.Name, p.Framework)
		}
	}
	if len(res.Details) != 18 {
		t.Errorf("%d networks, want 18", len(res.Details))
	}
}
