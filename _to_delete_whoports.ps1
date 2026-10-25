foreach ($p in 8000, 8101, 7860) {
  "=== port $p"
  Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
      Get-CimInstance Win32_Process -Filter "ProcessId=$_" |
        ForEach-Object { "PID $($_.ProcessId) parent $($_.ParentProcessId): $($_.CommandLine)" }
    }
}
