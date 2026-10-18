# Brother Printer Manager — notes for contributors (and coding agents)

Open-source Windows tray app replacing Brother's software suite. Python 3.12+, uv, src layout (`src/printmgr`).
Public repo: https://github.com/giavanni6/brother-printer-manager (MIT).

## Gate
`uv run pytest` must be green. `tests/fakes.py` is an in-process fake printer (IPP + eSCL over HTTP, SNMP over UDP).
CI (`.github/workflows/test.yml`) runs the same gate on windows-latest.
Ship: `uv run python build.py` → `dist/PrinterManager/`. It's a one-folder build: 0.55 s to first status vs 1.2 s for one-file,
and no `%TEMP%\_MEI*` leftovers. Releases attach that folder zipped as `BrotherPrinterManager-windows.zip`.
Quit a running copy before overwriting it.

## Layout
- `ipp.py`, `snmp.py`, `escl.py`: hand-rolled protocol clients. Keep them stdlib-only; don't add pysnmp or similar.
- `status.py`: merges IPP (state, toner) with SNMP (drum, page count); `AlertTracker` notifies only on new conditions.
- `app.py`: Tk owns the main thread. pystray and the poller run in threads and hand UI work back with `App.post()`.

## Real-hardware facts (MFC-L2760DW, verified 2026-10-17)
Other models may differ. Add a section when you verify one.

- IPP `/ipp/print` reports one toner marker named `BK`, with its own low level at 16%. The drum comes only from
  SNMP (`Drum Unit`, max 15000). SNMP toner level is -3 ("some remaining"), so IPP wins for toner.
- eSCL is at `http://<ip>:80/eSCL`, version 2.93. Platen and ADF; **no duplex scan**; 100/200/300/600 dpi.
- Its ScanSettings parser is strict. **Whitespace between XML elements makes it silently scan at 200 dpi in color.**
  `build_scan_settings` therefore sends compact XML.
- `BlackAndWhite1` + JPEG → 409, so B&W is scanned as grayscale and thresholded locally (saved as 1-bit PNG/PDF).
- An empty ADF → HTTP 500 (not 409). `ScannerStatus` `AdfState=ScannerAdfEmpty` is how we tell.
- A job started while another runs gets 409, 500 or 503 while `ScannerStatus` says Processing. An abandoned job
  (no NextDocument or DELETE) keeps it busy for about 60 s, then the printer drops the job itself.
  `_create_job` retries for up to 90 s while the scanner is not Idle.
- 600 dpi color letter: 34 s per page.

## Reliability invariants (see VERIFICATION.md; regressions in tests/test_reliability.py)
- `net.request`: every failure is an OSError, and `timeout` bounds the whole request (a watchdog shuts the socket down).
- Worker threads catch `Exception` and always post back to their window. Config writes go only through `App.update_cfg()`.
- `config.load` never raises: bad files fall back per field, and an unreadable file is kept as `config.json.bad`.
- `Add-Printer -IppURL` works without admin. Windows labels the port `WSD-…`, so `find_installed` matches by name.
