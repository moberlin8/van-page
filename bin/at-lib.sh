#!/bin/sh
# ## Ada 22-Oct-2026 AT-command helper for the Quectel modem on Delilah.
# Talks to the modem's own tty WITHOUT holding a long-lived fd: open the port,
# write, read, close, every call. That avoids two real hazards on this router:
#   * a shared fd across a command substitution races with the shell's own use
#     of the descriptor, which is what made an earlier version return nothing;
#   * GL.iNet's modem_manager holds the same port and interleaves ERROR replies,
#     so each command is retried and accepted only when it carries its own
#     "+TAG:" marker.
# Usage: at_read <port> <command> [expected_tag]
# Prints the response line(s) on stdout, returns 0 when the marker matched.
AT_READ_RETRIES=6

at_read() {
  _port=$1; _cmd=$2; _tag=${3:-}
  _i=0
  while [ "$_i" -lt "$AT_READ_RETRIES" ]; do
    {
      # open, write, read with a timeout, then close -- nothing held open
      eval "exec 3<>\"\$_port\"" 2>/dev/null || return 1
      printf 'AT+%s\r' "$_cmd" >&3 2>/dev/null
      timeout 2 cat <&3 2>/dev/null | tr -d '\r'
      eval "exec 3<&-" 2>/dev/null
    } > /tmp/.at_out.$$ 2>/dev/null
    if [ -n "$_tag" ]; then
      if grep -q "+$_tag:" /tmp/.at_out.$$ 2>/dev/null; then
        cat /tmp/.at_out.$$; rm -f /tmp/.at_out.$$; return 0
      fi
    elif grep -q "OK" /tmp/.at_out.$$ 2>/dev/null; then
      cat /tmp/.at_out.$$; rm -f /tmp/.at_out.$$; return 0
    fi
    _i=$((_i + 1))
    sleep 0.3
  done
  cat /tmp/.at_out.$$ 2>/dev/null
  rm -f /tmp/.at_out.$$
  return 1
}

# one-shot probe: is anything answering on this port?
# Retries, because the first attempt frequently returns ERROR purely because
# GL.iNet's modem_manager answered instead of the modem.
at_alive() {
  _port=$1
  _i=0
  while [ "$_i" -lt "$AT_READ_RETRIES" ]; do
    _r=$(at_read "$_port" "AT" 2>/dev/null)
    case "$_r" in
      *OK*) return 0 ;;
      *"+CSQ:"*) return 0 ;;
    esac
    _i=$((_i + 1))
    sleep 0.3
  done
  # last resort: any non-ERROR answer means the port is live
  case "$_r" in
    *ERROR*|"") return 1 ;;
    *) return 0 ;;
  esac
}
