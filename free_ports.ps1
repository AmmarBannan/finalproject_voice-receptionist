# Stops whatever is listening on the ports this project uses, so start.bat can bind them.
foreach ($p in 8000, 8101, 7860) {
  Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
      $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$_"
      "port ${p}: stopping PID $_ $($proc.CommandLine)"
      Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
    }
}
