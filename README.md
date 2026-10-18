# Brother Printer Manager

A small, open-source Windows tray app for Brother network printers and all-in-ones, replacing
Brother's full software package. It installs no Brother drivers and no background services, and it
uses no Brother software at all.

Everything goes through open standards that the printer already speaks:

| Feature | How |
|---|---|
| Live status: ready / printing / errors | IPP `Get-Printer-Attributes` (port 631) |
| Toner level, drum life, page count | IPP marker attributes + SNMP Printer-MIB (RFC 3805) |
| Scan to PDF or JPEG from the flatbed or document feeder | eSCL / AirScan (driverless scanning) |
| Notifications for low toner, paper jam, cover open, out of paper, offline | Windows toasts, shown once per new condition |
| One-click "install in Windows" | `Add-Printer -IppURL` with Windows' built-in **Microsoft IPP Class Driver** |
| Finds the printer automatically, including after its IP changes | mDNS/Bonjour, falling back to a subnet sweep |

> **Unofficial.** This project is not affiliated with, endorsed by, or supported by Brother
> Industries, Ltd. "Brother" is a trademark of its owner and is used here only to describe
> which printers the app works with.

## Supported printers

The app should work with any Brother printer or all-in-one that supports **AirPrint / IPP
Everywhere**, and with **AirScan (eSCL)** for scanning. That covers most network models from roughly
2015 onward.

| Model | Status |
|---|---|
| MFC-L2760DW | Tested: status, toner and drum, flatbed scan, feeder scan, driverless install |

If you try another model, please open an issue saying what works. Hardware differences are documented in
[CLAUDE.md](CLAUDE.md#real-hardware-facts-mfc-l2760dw-verified-2026-10-17). For example, the
MFC-L2760DW silently ignores scan settings sent as indented XML.

## Install

1. Download `BrotherPrinterManager-windows.zip` from the [latest release](../../releases/latest) and unzip it anywhere,
   for example `%LOCALAPPDATA%\Programs\BrotherPrinterManager`.
2. Run `PrinterManager.exe`. A printer icon appears in the system tray, and the app finds your printer by itself.
   If it doesn't, right-click the icon, choose **Find printer…** and type the printer's IP address.
3. Optional: right-click the icon, then **Install printer in Windows**, so you can print from any app with Windows'
   own driver.
4. Optional: right-click the icon and tick **Start with Windows**.

The badge on the tray icon shows the printer's state: green = ready, amber = low supply or warning,
red = error, gray = offline. Double-click the icon for the status window.

Settings and the log live in `%APPDATA%\PrinterManager\`.

**Tip:** if you use third-party toner, turn off automatic firmware updates in the printer's web settings
(tray menu → **Printer web settings**). Brother firmware updates can block non-Brother cartridges.

## Build from source

Requires Windows, Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest            # 66 tests against an in-process fake printer (IPP + eSCL + SNMP)
uv run python -m printmgr
uv run python build.py   # -> dist\PrinterManager\ (PyInstaller one-folder build)
```

The protocol clients (`ipp.py`, `snmp.py`, `escl.py`) are small, stdlib-only and hand-rolled. The only
runtime dependencies are `pystray`, `Pillow` and `zeroconf`. See [VERIFICATION.md](VERIFICATION.md) for the
adversarial reliability testing the app has been through.

## License

[MIT](LICENSE)
