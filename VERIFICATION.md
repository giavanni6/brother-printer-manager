# Verification record — 2026-10-18

Adversarial verification ran in two rounds. Each round used independent verifier agents, and each verifier tried to prove claims false. The final state is 66 tests passing, all reported defects fixed, and the build reinstalled.

## Round 1 — three parallel verifiers

| Area | Result |
|---|---|
| Feature completeness (real printer) | 6 PASS / 2 PARTIAL |
| Code reliability (fakes, fuzzing 60k inputs per parser) | FAIL: 13 findings |
| Live-hardware reliability (MFC-L2760DW) | 5 PASS / 2 FAIL |

**What held up in round 1:**
- The parsers never crashed on fuzzed input.
- No leaks over 300 polls or 30 scans.
- Every Tk call is on the main thread.
- PowerShell injection is blocked.
- Status polling is consistent: 30 polls in a row, 13 SNMP round trips each, 0 packet loss.
- Polling every 60 s does not keep the printer awake.
- Memory stays flat over time.
- HTTPS eSCL works.

### Defects found and fixed

**Network: every failure is now an OSError, and `timeout` bounds the whole request**
- A server sending the body slowly could stall status checks forever.
- A server sending headers slowly could do the same; this was caught in round 2.
- `IncompleteRead`, `BadStatusLine` and `InvalidURL` escaped as non-OSError, so a stale icon stayed up and windows got stuck.
- A body that ended early was accepted silently, so a scan cut off by a Wi-Fi drop saved a corrupt image. A new test caught this one.

**SNMP**
- A stream of datagrams with the wrong request id could stall a read forever.
- One malformed datagram aborted the read.
- Replies were not checked against the sender's address.

**Status values**
- An out-of-band IPP marker value (decoded as bytes) caused a `TypeError` on every poll.
- An absurd level caused an overflow.
- A low threshold of 100% or more caused a permanent false "low" alert.

**Alerts**
- One missed poll caused an offline toast, and on reconnect every low-supply alert was repeated.
- Every login re-announced alerts that were already shown.

**Config**
- A config file holding `[]` or `null` crashed the app at startup.
- Wrong value types silently killed the poller.
- An ANSI or UTF-16 file (Notepad) crashed the app at startup.
- A corrupt file was silently overwritten.
- Two saves at the same moment raised `PermissionError`.

**Lost update** (introduced in round 1, caught in round 2)
- A scan-settings save that raced a printer change lost one of the two changes.
- Every config write now goes through `App.update_cfg()` under a lock.

**UI**
- The Scan, Find, Search and Install worker threads could leave windows stuck on "Scanning…" or "Connecting…" forever.
- The 2-sided option and 150 dpi were offered before the scanner reported its capabilities.
- Legal-size scans on the flatbed were cropped without telling the user.

**Scanner (real hardware)**
- A second scan started while one was running got HTTP 500, and the app gave up immediately.
- After an abandoned job, the printer stays busy for about 60 s, which was longer than the 30 s retry window.
- Both now retry for up to 90 s and show "Waiting for the scanner…". Re-verified on the real printer: the second scan of a concurrent pair took 19 s, and a scan after an abandoned job took 67 s.

**Install**
- An address like 192.168.0.7 also matched a port for 192.168.0.76.
- Elevation was requested on any failure; it now happens only on access-denied.
- A timeout escaped as an unhandled error.
- A stopped Print Spooler read as "not installed".

**Autostart**
- Running a `dist/` build could re-point the Run entry. The entry is now repaired only when its target exe is missing.

## Round 2 — re-verification of the fixes
- 6 fixes confirmed.
- 2 fixes were incomplete: the slow-header deadline and non-UTF-8 config files.
- 1 regression: the lost update.
- All of these are fixed and covered by `tests/test_reliability.py`, along with the minor items it noted.
- The open hardware question was answered: a 600 dpi color page takes 34 s, well within the 180 s page limit.

## Known limits (accepted)
- DNS lookups are not bounded by the request deadline. The app addresses the printer by IP, so this does not come up in practice.
- `find_installed` matches the first queue with the model code. If a second identical printer or a fax queue existed, it could pick the wrong one. Neither exists here.
