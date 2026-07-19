using System;
using System.Collections.Generic;
using System.Diagnostics;

namespace MyAgentAdminPanel.Services;

public class KubectlService
{
    private readonly Dictionary<string, Process> _processes = new();

    public event Action<string, string>? OutputReceived; // (key, line)

    public Process? StartPortForward(string key, string ns, string svc, int localPort, int remotePort)
    {
        StopPortForward(key);

        var psi = new ProcessStartInfo
        {
            FileName = "kubectl",
            Arguments = $"-n {ns} port-forward --address 0.0.0.0 svc/{svc} {localPort}:{remotePort}",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        var proc = new Process { StartInfo = psi };
        proc.OutputDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                OutputReceived?.Invoke(key, e.Data);
        };
        proc.ErrorDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                OutputReceived?.Invoke(key, $"[ERR] {e.Data}");
        };

        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        _processes[key] = proc;
        return proc;
    }

    public void StopPortForward(string key)
    {
        if (_processes.TryGetValue(key, out var proc))
        {
            try
            {
                if (!proc.HasExited)
                {
                    proc.Kill(entireProcessTree: true);
                    proc.WaitForExit(3000);
                }
            }
            catch { }
            _processes.Remove(key);
        }
    }

    public void StopAll()
    {
        foreach (var key in _processes.Keys)
            StopPortForward(key);
        _processes.Clear();
    }

    public bool IsRunning(string key)
        => _processes.TryGetValue(key, out var proc) && !proc.HasExited;

    public string RunCommand(string arguments)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "kubectl",
            Arguments = arguments,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        var proc = Process.Start(psi)!;
        var output = proc.StandardOutput.ReadToEnd();
        var err = proc.StandardError.ReadToEnd();
        proc.WaitForExit();
        return string.IsNullOrEmpty(err) ? output : $"{output}\n{err}";
    }
}
