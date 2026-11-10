
## Photos and receipts

Repair jobs and receipts carry images. Uploads are downscaled to 2000px on the
longest edge (Pillow), EXIF rotation is applied so portrait shots aren't
sideways, and files are served through `/media/<key>` behind the password gate.

Railway wipes the container filesystem on every deploy, so **a volume is
required** or photos vanish on the next push:

1. In the app service, Settings → Volumes → add a volume, mount path `/data`.
2. Add a variable `MEDIA_DIR=/data/media`.

Locally, `MEDIA_DIR` defaults to a `media/` folder in the repo, which
`.gitignore` excludes.

Screens added: `/jobs` (open and finished repairs), `/jobs/new`, and
`/jobs/<id>` with before/after photos and editable comments. Receipt photos
attach from the expense form or from any ledger row.
