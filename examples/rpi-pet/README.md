# Cosmergon Pet — moved

The Cosmergon Pet (Raspberry Pi + OLED + rotary encoder build) has its
own repository:

**[github.com/rkocosmergon/cosmergon-pet](https://github.com/rkocosmergon/cosmergon-pet)**

It ships with:

- Full build guide (PDF + markdown)
- Hardware BOM with shop links (Amazon, Völkner)
- Installer + systemd unit for autostart
- Troubleshooting, FAQ, pinout diagrams

## Quick start

```bash
python3 -m venv ~/cosmergon-env
source ~/cosmergon-env/bin/activate
pip install git+https://github.com/rkocosmergon/cosmergon-pet
cosmergon-pet
```

Or with a one-line installer on a fresh Raspberry Pi, see the Pet repo.

## Why a separate repo?

The Pet is a maker project with its own build cycle, hardware concerns
and contributor community. Keeping it next to the Python SDK made sense
while bootstrapping; it now lives on its own. This README stays here to
redirect anyone who arrives via an old link or older build guide.
