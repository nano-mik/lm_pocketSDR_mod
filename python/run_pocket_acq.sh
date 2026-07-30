#!/usr/bin/env bash
set -euo pipefail

# pocket_acq_custKasami.py defaults
PYTHON_BIN="${PYTHON_BIN:-python3}"
ACQ_SCRIPT="${ACQ_SCRIPT:-./pocket_acq_custKasami.py}"
FREQ_MHZ="16.368"
IF_MHZ="4.013"
IQ_MODE="2"
PRN="1"
LOG_ENABLED=1
TIME_OFFSET_MS="100"
INTEGRATION_MS="30"
SDR_TYPE="gnuradio"
WM_FILE="${HOME}/SDR_Recording/remote/regus_test_1500M_wm_P4_-44.50dBm_G30.0dB_20260730_163919_20260730-163921-838.c64"
INTERACTIVE=0
DRY_RUN=0
GUI=0

usage() {
    cat <<'EOF'
Usage: ./run_pocket_acq.sh [options]

Options:
  -i, --interactive       Prompt for all acquisition values
  -g, --gui               Open the graphical argument editor
  -f, --freq MHz          Sampling frequency         (default: 16.368)
      --if MHz            Intermediate frequency     (default: 4.013)
      --iq MODE           IQ mode                    (default: 2)
  -p, --prn NUMBER        PRN number                 (default: 1)
      --log               Enable -l                  (default)
      --no-log            Omit -l
      --toff MS           Time offset                (default: 100)
      --tint MS           Integration time           (default: 30)
      --sdr NAME          SDR input type              (default: gnuradio)
  -w, --wm FILE           Watermark recording file
      --script FILE       Acquisition Python script
  -n, --dry-run           Print the command without running it
  -h, --help              Show this help

Environment overrides:
  PYTHON_BIN              Python executable (default: python3)
  ACQ_SCRIPT              Acquisition script path

Examples:
  ./run_pocket_acq.sh -i
  ./run_pocket_acq.sh --gui
  ./run_pocket_acq.sh --prn 4 --tint 50 --wm recording.c64
  ./run_pocket_acq.sh --dry-run
EOF
}

need_value() {
    if (($# < 2)); then
        echo "Error: $1 requires a value." >&2
        exit 2
    fi
}

while (($#)); do
    case "$1" in
        -i|--interactive) INTERACTIVE=1; shift ;;
        -g|--gui)         GUI=1; shift ;;
        -f|--freq)        need_value "$@"; FREQ_MHZ="$2"; shift 2 ;;
        --if)             need_value "$@"; IF_MHZ="$2"; shift 2 ;;
        --iq)             need_value "$@"; IQ_MODE="$2"; shift 2 ;;
        -p|--prn)         need_value "$@"; PRN="$2"; shift 2 ;;
        --log)            LOG_ENABLED=1; shift ;;
        --no-log)         LOG_ENABLED=0; shift ;;
        --toff)           need_value "$@"; TIME_OFFSET_MS="$2"; shift 2 ;;
        --tint)           need_value "$@"; INTEGRATION_MS="$2"; shift 2 ;;
        --sdr)            need_value "$@"; SDR_TYPE="$2"; shift 2 ;;
        -w|--wm)          need_value "$@"; WM_FILE="$2"; shift 2 ;;
        --script)         need_value "$@"; ACQ_SCRIPT="$2"; shift 2 ;;
        -n|--dry-run)     DRY_RUN=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *) echo "Error: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if ((GUI)); then
    GUI_SCRIPT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/run_pocket_acq_gui.py"
    if [[ ! -f "$GUI_SCRIPT" ]]; then
        echo "Error: GUI script not found: $GUI_SCRIPT" >&2
        exit 1
    fi
    exec "$PYTHON_BIN" "$GUI_SCRIPT"
fi

prompt_value() {
    local label="$1"
    local current="$2"
    local entered
    read -r -p "${label} [${current}]: " entered
    printf '%s' "${entered:-$current}"
}

if ((INTERACTIVE)); then
    FREQ_MHZ="$(prompt_value "Sampling frequency (MHz)" "$FREQ_MHZ")"
    IF_MHZ="$(prompt_value "Intermediate frequency (MHz)" "$IF_MHZ")"
    IQ_MODE="$(prompt_value "IQ mode" "$IQ_MODE")"
    PRN="$(prompt_value "PRN" "$PRN")"
    TIME_OFFSET_MS="$(prompt_value "Time offset (ms)" "$TIME_OFFSET_MS")"
    INTEGRATION_MS="$(prompt_value "Integration time (ms)" "$INTEGRATION_MS")"
    SDR_TYPE="$(prompt_value "SDR type" "$SDR_TYPE")"
    WM_FILE="$(prompt_value "Watermark file" "$WM_FILE")"

    read -r -p "Enable logging? [Y/n]: " log_answer
    case "${log_answer:-y}" in
        y|Y|yes|YES) LOG_ENABLED=1 ;;
        *)           LOG_ENABLED=0 ;;
    esac
fi

if ((!DRY_RUN)) && [[ ! -f "$ACQ_SCRIPT" ]]; then
    echo "Error: acquisition script not found: $ACQ_SCRIPT" >&2
    exit 1
fi

if ((!DRY_RUN)) && [[ ! -f "$WM_FILE" ]]; then
    echo "Error: watermark file not found: $WM_FILE" >&2
    exit 1
fi

command=(
    "$PYTHON_BIN" "$ACQ_SCRIPT"
    -f "$FREQ_MHZ"
    -fi "$IF_MHZ"
    -IQ "$IQ_MODE"
    -prn "$PRN"
)

if ((LOG_ENABLED)); then
    command+=(-l)
fi

command+=(
    -toff "$TIME_OFFSET_MS"
    -tint "$INTEGRATION_MS"
    -sdr "$SDR_TYPE"
    -wm "$WM_FILE"
)

printf 'Command:'
printf ' %q' "${command[@]}"
printf '\n'

if ((DRY_RUN)); then
    exit 0
fi

exec "${command[@]}"
