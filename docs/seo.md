# Search engine optimisation

Search engines are the main way buyers are expected to arrive, so the pages are built for them. The code is in
`app/seo.py` and `app/routes/seo_pages.py`.

- **Landing pages for real search phrases:** `/factory-for-sale/shah-alam`, `/warehouse-for-rent/port-klang`,
  `/land-for-sale/johor`, for every type, deal, town and state. Each has its own title, heading and an intro written from
  the live listings: count, price range, land sizes, power and tenure, plus the Malay phrase. Empty pages are `noindex`.
- **Readable listing URLs:** `/listings/<id>/detached-factory-with-600a-power-in-shah-alam`. Short or outdated links
  redirect with a 301 to the one canonical URL. Ended listings return 410, so they drop out of the index quickly.
- **Every public page** has a title, meta description, canonical link, Open Graph and Twitter tags, and a language tag.
- **Structured data (JSON-LD):** `RealEstateListing` with price, address, coordinates and specs on each listing;
  `BreadcrumbList`; `ItemList` on landing pages; `WebSite` and `Organization` on the home page.
- **`/sitemap.xml`** lists the home page, every listing with its photos, and every landing page that has listings.
  **`/robots.txt`** blocks `/agent` and `/api/` and points to the sitemap.
- **Kept out of the index:** filter combinations (`noindex, follow`), agent pages (`X-Robots-Tag`) and empty landing
  pages.
- **Speed:** gzip, year-long caching of versioned static files and photos, image dimensions to prevent layout shift, and
  the map library loaded only on pages with a map.
- **Internal links:** breadcrumbs, "nearby" links on landing pages, and a "popular searches" footer.

After deploying, set `SITE_URL` to the real domain and submit `https://<domain>/sitemap.xml` to Google Search Console.
