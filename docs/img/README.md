# Screenshots

The root `README.md` links to two images from this directory. Drop them in with
**exactly these filenames** and they render automatically — no markdown changes needed.

All four come from `notebooks/Lakebase-Tables-And-Records.ipynb` except the first.

| filename | what to capture |
|---|---|
| `app.png` | The deployed app with a ticket selected, so the message thread is visible. Keep the URL bar in frame (`…databricksapps.com`) — that is what shows it is deployed rather than localhost. The stat tiles across the top demonstrate the statistics bonus for free. |
| `lakebase-tables.png` | The **"Tables, row counts, and the foreign key"** cell. The foreign-key row is the important one: it shows `ticket_messages.ticket_id → tickets.ticket_id` with `delete_rule CASCADE`, which is the relationship requirement proven rather than asserted. |
| `lakebase-tickets.png` | The **"All tickets"** cell. Widen the `ticket_id` column first — it defaults narrow enough to clip its own header and render `21` as `.1`. |
| `lakebase-messages.png` | The **"All messages"** cell, showing every message under its ticket title. |

> **Mask the workspace ID.** A Databricks App URL looks like
> `lakebase-support-app-<workspace-id>.<region>.databricksapps.com`, and that number is
> also embedded in your workspace hostname. It is not a credential — Apps still require
> authentication, so nobody gets in with it — but this repository is public, and blurring
> the digits costs nothing. `lakebase-support-app-████.aws.databricksapps.com` still proves
> the app is deployed. The same ID appears in the browser URL of any notebook screenshot.
| `lakebase.png` | Lakebase tables and records. Easiest source: pull the repo in Databricks, run `notebooks/Lakebase-Tables-And-Records.ipynb`, and capture the **"All tickets"** cell — 6 tickets with status, priority, category and message counts. Keep the notebook path visible at the top so it is clear this ran inside Databricks against Lakebase. |

Optional third image, if you want to show the schema and the foreign key:
`schema.png`, from the **"Tables, row counts, and the foreign key"** cell of the same
notebook. Add it to the README yourself — it is not linked by default.

## Tips

* On macOS, `Cmd+Shift+4` then `Space` captures a single window cleanly, without the
  desktop behind it.
* Crop to the content. A full-screen retina capture is mostly browser chrome and makes
  the interesting part small.
* Keep each file under ~500KB. PNG is right for UI and tables; JPEG blurs small text.
* Check no credential is visible before committing — a connection string in a terminal
  window behind the app, or a secret value in a notebook cell output, is a real leak that
  is hard to take back once pushed.
