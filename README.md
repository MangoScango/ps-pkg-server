# PS PKG Server
![](/screenshot.png)

A simple HTTP server for self hosting PS4 & PS5 packages. Pulls metadata out of each package for easy content identification regardless of file name. Transparently serves split packages as one contiguous pkg. Identifies fpkg updates not married to the base game. Supports pushing directly to consoles running [MangoScango/ps5-ezremote-dpi](https://github.com/MangoScango/ps5-ezremote-dpi) or similar services.


See [docker-compose.yml](/docker-compose.yml) for Docker deployment. To run on bare metal:

```
set PKG_DIRS=D:\PS4\pkgs
py -m pip install -r requirements.txt
py -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Then open http://localhost:8000.


| Variable | Purpose | Default |
|---|---|---|
| `PKG_LIBRARY` | Host path to your PKG library (mounted read-only at `/pkgs`) | `./pkgs` |
| `PKG_DIRS` | Colon-separated dirs to scan *inside* the container | `/pkgs` |
| `SCAN_WORKERS` | Parallel parse workers | `8` |
| `PUBLIC_HOST` | Force the `host:port` the console downloads from | auto-detect |
| `ENTITLEMENTS_CSV` | Entitlement catalogue for cloud install | `entitlements_all.csv` |

- Icons and other runtime data persist in `./data` (mounted at `/data`).
- Add more libraries by adding `:ro` volumes in `docker-compose.yml` and listing
  them all in `PKG_DIRS` (e.g. `/pkgs:/pkgs2`).


## Cloud install

You can push retail titles directly from the CDN to your console with the cloud icon in the header. If you contribute your entitlements database to [Garlic Saves](https://garlicsaves.com/tools/entitlements), you can export the result and search through it dynamically. Afterwards upload just the `entitlements_all.csv` to the server or drop it in the scripts working directory. Without a catalogue you can still paste a direct `.pkg`, `.json` manifest, or `version.xml` URL into the search box to install from it. Updates from [OrbisPatches](https://www.orbispatches.com) will work automatically, just paste the link for any pkg piece and the server will push the full update. For PS5, use the `SC` pkg link provided by [ProsperoPatches](https://prosperopatches.com/).

## Special Thanks

[OpenOrbis/LibOrbisPkg](https://github.com/OpenOrbis/LibOrbisPkg) for PS4 Pkg Format Reference

[hippie68/msum](https://github.com/hippie68/msum) for PS4 Update Pkg Marriage Checksum 

[SvenGDK/LibProsperoPKG](https://github.com/SvenGDK/LibProsperoPKG) for PS5 Pkg Format Reference 

