using System;
using System.Diagnostics;

namespace MyAgentAdminPanel.Services;

public class DockerService
{
    public event Action<string>? OutputReceived;

    public Process BuildImage(string imageName, string tag, string contextDir)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "docker",
            Arguments = $"build -t agent/{imageName}:{tag} {contextDir}",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        var proc = new Process { StartInfo = psi };
        proc.OutputDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                OutputReceived?.Invoke(e.Data);
        };
        proc.ErrorDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                OutputReceived?.Invoke($"[ERR] {e.Data}");
        };

        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        return proc;
    }

    public string? RunCommand(string arguments)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "docker",
            Arguments = arguments,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        var proc = Process.Start(psi)!;
        var output = proc.StandardOutput.ReadToEnd();
        proc.WaitForExit();
        return output;
    }
}
