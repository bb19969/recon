name: Recon Sweep (clone tool at runtime)

on:
  workflow_dispatch: {}

permissions:
  contents: read

jobs:
  recon:
    runs-on: ubuntu-latest
    timeout-minutes: 180

    env:
      TOOL_REPO: ${{ vars.TOOL_REPO }}   # e.g. https://github.com/you/priority_scopes
      TOOL_PATH: ${{ vars.TOOL_PATH }}   # e.g. priority_scopes.py
      # Optional secret:
      DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}

    steps:
      - name: Checkout this repo
        uses: actions/checkout@v4

      - name: Python & deps
        run: |
          sudo apt-get update -y
          sudo apt-get install -y python3 python3-pip
          pip3 install --upgrade pip
          pip3 install requests pyyaml

      - name: Add swap (2G)
        run: |
          sudo swapoff -a || true
          sudo fallocate -l 2G /swapfile
          sudo chmod 600 /swapfile
          sudo mkswap /swapfile
          sudo swapon /swapfile

      - name: Install PD tools (prebuilt)
        env:
          ARCH: ${{ runner.arch }}
        run: |
          set -e
          if [ "${ARCH}" = "ARM64" ]; then PAT="linux_arm64"; else PAT="linux_amd64"; fi
          install_pd () {
            repo="$1"
            tmp="$(mktemp -d)"; cd "$tmp"
            url="$(curl -s https://api.github.com/repos/projectdiscovery/${repo}/releases/latest \
              | grep -Eo 'browser_download_url":\s*"[^"]*'"$PAT"'[^"]*\.zip"' \
              | cut -d\" -f4 | head -n1)"
            wget -q "$url" -O r.zip
            unzip -q r.zip
            sudo install -m0755 "$repo" "/usr/local/bin/$repo"
            cd - >/dev/null
            rm -rf "$tmp"
          }
          sudo apt-get install -y curl unzip ca-certificates >/dev/null 2>&1 || true
          install_pd subfinder
          install_pd httpx
          install_pd nuclei
          install_pd katana
          # Amass
          tmp="$(mktemp -d)"; cd "$tmp"
          wget -q "https://github.com/owasp-amass/amass/releases/latest/download/amass_${PAT}.zip" -O amass.zip
          unzip -q amass.zip
          sudo install -m0755 amass /usr/local/bin/amass
          cd - >/dev/null; rm -rf "$tmp"
          # Findomain
          tmp="$(mktemp -d)"; cd "$tmp"
          FURL="$(curl -s https://api.github.com/repos/findomain/findomain/releases/latest \
            | grep -Eo 'browser_download_url":\s*"[^"]*linux[^"]*(zip|tar\.gz)\"' | cut -d\" -f4 | head -n1)"
          if echo "$FURL" | grep -q '\.zip$'; then
            wget -q "$FURL" -O f.zip
            unzip -q f.zip
            sudo install -m0755 findomain* /usr/local/bin/findomain
          else
            wget -q "$FURL" -O findomain
            sudo install -m0755 findomain /usr/local/bin/findomain
          fi
          cd - >/dev/null; rm -rf "$tmp"

      - name: Small tools (assetfinder/waybackurls/gf) + templates
        run: |
          sudo apt-get install -y golang >/dev/null 2>&1 || true
          export GOTOOLCHAIN=local
          go install github.com/tomnomnom/assetfinder@latest >/dev/null 2>&1 || true
          go install github.com/tomnomnom/waybackurls@latest >/dev/null 2>&1 || true
          go install github.com/tomnomnom/gf@latest >/dev/null 2>&1 || true
          sudo ln -sf "$HOME/go/bin/assetfinder" /usr/local/bin/assetfinder || true
          sudo ln -sf "$HOME/go/bin/waybackurls" /usr/local/bin/waybackurls || true
          sudo ln -sf "$HOME/go/bin/gf" /usr/local/bin/gf || true
          nuclei -update-templates >/dev/null 2>&1 || true

      - name: Clone tool repo (runtime)
        run: |
          set -e
          mkdir -p tools && cd tools
          echo "[*] Cloning: ${TOOL_REPO}"
          git clone --depth 1 "${TOOL_REPO}" toolsrc
          cd -
          if [ ! -f "tools/toolsrc/${TOOL_PATH}" ]; then
            echo "[-] Could not find tools/toolsrc/${TOOL_PATH}"
            exit 1
          fi

      - name: Seed targets.txt if missing
        run: |
          if [ ! -s targets.txt ]; then
            echo "[i] targets.txt missing or empty — seeding with example.com"
            echo "example.com" > targets.txt
          fi
          sed -n '1,10p' targets.txt

      - name: Configure Discord (optional)
        if: ${{ env.DISCORD_WEBHOOK != '' }}
        run: |
          python3 - <<'PY'
import yaml, os, pathlib
p=pathlib.Path('config.yaml'); cfg=yaml.safe_load(p.read_text()) if p.exists() else {}
cfg['discord_webhook']=os.environ['DISCORD_WEBHOOK']
cfg['notify']={'on_new_targets':True,'on_findings':True}
cfg.setdefault('nuclei', {'enable': True, 'templates': '~/.local/share/nuclei-templates'})
p.write_text(yaml.safe_dump(cfg, sort_keys=False))
print("[i] webhook saved")
PY

      - name: Run tool (verbose; don’t fail job)
        run: |
          set -o pipefail
          mkdir -p recon_out logs
          echo "[*] Running tools/toolsrc/${TOOL_PATH}" | tee -a logs/run.log
          python3 -u "tools/toolsrc/${TOOL_PATH}" \
            --mode full --min-score 0 --top 15 \
            --run --outdir recon_out \
            --global-merge --global-nuclei --global-nuclei-shards 2 \
            --global-crawl --global-gf 2>&1 | tee -a logs/run.log || echo "[!] tool exited non-zero" | tee -a logs/run.log
          # always continue so artifacts upload
          true

      - name: List outputs (debug)
        run: |
          echo "[i] targets.txt:"
          sed -n '1,50p' targets.txt || true
          echo "[i] recon_out tree:"
          find recon_out -type f -maxdepth 3 2>/dev/null | sed -n '1,200p' || true
          echo "[i] logs:"
          find logs -type f -maxdepth 2 2>/dev/null | sed -n '1,200p' || true

      - name: Upload artifacts (always)
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: recon-results-${{ github.run_id }}
          path: |
            targets.txt
            recon_out/**/*
            logs/**/*
          if-no-files-found: warn
          retention-days: 7
