# Memory controls

The admin now aggregates analytics in SQL, loads stats only where displayed and paginates article bodies out of lists. Media listings use a persistent metadata catalog; an initial background scan processes 500 R2 objects per page, records its cursor and resumes after interruption. Uploads update the catalog independently of editorial transactions. Reconcile externally added objects with `flask --app wsgi index-media`.

Media endpoints advertise `X-Media-Upload: multipart` on OPTIONS and accept an `image` or `video` file with form fields. Legacy JSON clients remain supported with size limits. Downloads stream in 64 KiB chunks and preserve video Range requests. Public media redirects directly to R2.

Only one heavy generation runs per process. Concurrent requests receive 429 and Retry-After before their bodies are parsed. Retain the existing single Gunicorn worker: the in-process admission lock is not a distributed queue. No extra always-on service was added. FFmpeg uses two codec threads and one filter-complex thread; output is uploaded from disk. Image sources are bounded by bytes and pixels and are loaded once per variant batch; only selected formats are generated, with URL reuse within a request.

Configuration defaults: `IMAGE_MAX_BYTES=15000000`, `IMAGE_MAX_PIXELS=24000000`, `VIDEO_MAX_BYTES=83886080`, `FFMPEG_THREADS=2`, `DB_POOL_SIZE=4`, `DB_MAX_OVERFLOW=1`, `DB_POOL_TIMEOUT=10`. `MEDIA_INDEX_ON_START=0` disables reconciliation (useful for tests). PostgreSQL server buffers/connections and existing historical data are not modified. Safe startup diagnostics are logged as DB_RESOURCE_SNAPSHOT. Authenticated `/admin/api/resources` exposes process RSS/peak and pool state; `/health` is a lightweight release check.

Validation: `python -m pip install -r requirements-dev.txt`; `PYTHONPATH=. python -m pytest -q tests`. Integration tests use SQLite and a real local FFmpeg binary. Compare representative 24–72h memory windows, job peaks, errors and latency after release; initial fresh-process memory is not a sustained savings measurement. New catalog tables are additive; rollback to the previous application commit leaves original editorial tables intact.

## Editorial image compression

New panel/library uploads, WhatsApp article photos (multipart and legacy JSON) and downloaded mirror images pass through one bounded optimizer before local storage or R2 upload. Static raster photos are reduced to a 1280 px maximum edge and WebP quality 68/method 3; environment overrides are `IMAGE_WEB_MAX_EDGE` and `IMAGE_WEB_QUALITY`. Encoding happens once, serially per process; JPEG draft decoding, temporary output files and release of decoded pixels before network transfer bound memory. Small images retain original bytes if WebP would be larger. EXIF orientation and alpha are preserved in converted output, while camera metadata is removed. Animated images, SVG and optional HEIF/AVIF formats without installed codecs retain their originals. Existing 15 MB/24 MP admission limits remain.

Panel generator source uploads and generated PNG/social graphics explicitly bypass editorial conversion so their dimensions and output formats remain intact. Videos are unchanged. New UUID object keys have an immutable one-year public cache policy. Existing image URLs and historical R2 files are untouched. R2 transfer remains synchronous as before; compression timing excludes client upload and R2 network latency. `/health` reports `image_compression=webp-v1`.

Fixed R2 URL recognition to compare the host before treating external mirror images as already managed. Regression tests cover panel, both bot protocols, mirror uploads, actual boto transfer handling, size/pixel limits, alpha, orientation, animation, and generator/video preservation.
