// Slidecast.exe: a tiny launcher that opens the Slidecast window through pixi, without a console.
// install.ps1 compiles it with the C# compiler that ships with Windows (.NET Framework 4), so no
// binary lives in the repository. It runs `pixi run --manifest-path <its folder>\pixi.toml
// slidecast [args]`, i.e. exactly what a developer would type, in the real environment.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("Slidecast")]
[assembly: System.Reflection.AssemblyProduct("Slidecast - PowerPoint deck to narrated video")]
[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]

static class Launcher
{
    static string FindPixi()
    {
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var candidates = (Environment.GetEnvironmentVariable("PATH") ?? "")
            .Split(Path.PathSeparator)
            .Where(d => d.Length > 0)
            .Select(d => Path.Combine(d.Trim('"'), "pixi.exe"))
            .Concat(new[] {
                Path.Combine(local, "pixi", "bin", "pixi.exe"),
                Path.Combine(home, ".pixi", "bin", "pixi.exe"),
            });
        foreach (string c in candidates)
        {
            try { if (File.Exists(c)) return c; } catch (ArgumentException) { }
        }
        return null;
    }

    static string Quote(string a)
    {
        return a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '"' }) < 0
            ? a : "\"" + a.Replace("\"", "\\\"") + "\"";
    }

    static void Fail(string text, MessageBoxIcon icon)
    {
        MessageBox.Show(text, "Slidecast", MessageBoxButtons.OK, icon);
    }

    [STAThread]
    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string manifest = Path.Combine(dir, "pixi.toml");
        if (!File.Exists(manifest))
        {
            Fail("pixi.toml not found next to Slidecast.exe:\n" + dir, MessageBoxIcon.Error);
            return 1;
        }
        if (!Directory.Exists(Path.Combine(dir, ".pixi", "envs", "default")))
        {
            Fail("Slidecast is not installed yet.\n\nRun Install.cmd in\n" + dir,
                 MessageBoxIcon.Information);
            return 1;
        }
        string pixi = FindPixi();
        if (pixi == null)
        {
            Fail("pixi was not found. Run Install.cmd in\n" + dir, MessageBoxIcon.Error);
            return 1;
        }
        string rest = string.Join(" ", args.Select(Quote));
        // CreateNoWindow gives pixi a console with no window; the Python processes it starts
        // (the app, its window process, ffmpeg, LibreOffice) inherit it, so none flashes a
        // console window either.
        var psi = new ProcessStartInfo(pixi,
            "run --manifest-path " + Quote(manifest) + " slidecast " + rest)
        {
            WorkingDirectory = dir,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        try
        {
            Process.Start(psi);
        }
        catch (Exception e)
        {
            Fail("Could not start Slidecast:\n" + e.Message, MessageBoxIcon.Error);
            return 1;
        }
        return 0;
    }
}
