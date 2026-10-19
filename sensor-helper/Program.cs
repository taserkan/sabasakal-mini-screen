using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

namespace CS2Screen.SensorBridge;

internal sealed class UpdateVisitor : IVisitor
{
    public void VisitComputer(IComputer computer) => computer.Traverse(this);

    public void VisitHardware(IHardware hardware)
    {
        hardware.Update();
        foreach (IHardware child in hardware.SubHardware)
            child.Accept(this);
    }

    public void VisitSensor(ISensor sensor) { }
    public void VisitParameter(IParameter parameter) { }
}

internal static class Program
{
    private const double OwnerStaleSeconds = 12.0;

    private static IEnumerable<IHardware> Flatten(IHardware hardware)
    {
        yield return hardware;
        foreach (IHardware child in hardware.SubHardware)
            foreach (IHardware descendant in Flatten(child))
                yield return descendant;
    }

    // CPU sensors are resolved once and refreshed every minute (LHM can add
    // sensors after the first updates) instead of being re-enumerated with
    // LINQ every second.
    private const long SensorRefreshMilliseconds = 60_000;

    private static ISensor[] CpuSensors(Computer computer)
    {
        ISensor[] cpu = computer.Hardware
            .SelectMany(Flatten)
            .Where(hardware => hardware.HardwareType == HardwareType.Cpu)
            .SelectMany(hardware => hardware.Sensors)
            .ToArray();
        // LHM keeps a 24 h value history per sensor by default; only the
        // current value is used here, so the history would only grow memory.
        foreach (ISensor sensor in cpu)
            sensor.ValuesTimeWindow = TimeSpan.Zero;
        return cpu;
    }

    private static float? FirstValue(
        IEnumerable<ISensor> sensors, SensorType type, params string[] names)
    {
        foreach (string name in names)
        {
            ISensor? match = sensors.FirstOrDefault(sensor =>
                sensor.SensorType == type && sensor.Value is > 0 &&
                sensor.Name.Contains(name, StringComparison.OrdinalIgnoreCase));
            if (match?.Value is float value)
                return value;
        }
        return null;
    }

    private static float? AverageValue(
        IEnumerable<ISensor> sensors, SensorType type, Func<ISensor, bool> filter)
    {
        float[] values = sensors
            .Where(sensor => sensor.SensorType == type && sensor.Value is > 0 && filter(sensor))
            .Select(sensor => sensor.Value!.Value)
            .ToArray();
        return values.Length == 0 ? null : values.Average();
    }

    private static readonly string[] GameProcesses = { "cs2", "FlightSimulator2024" };
    private static DateTime _gameCheckedAt = DateTime.MinValue;
    private static bool _gameRunning;

    private static bool GameRunning()
    {
        if ((DateTime.UtcNow - _gameCheckedAt).TotalSeconds < 10)
            return _gameRunning;
        _gameCheckedAt = DateTime.UtcNow;
        bool running = false;
        foreach (string name in GameProcesses)
        {
            Process[] found = Process.GetProcessesByName(name);
            running |= found.Length > 0;
            foreach (Process process in found)
                process.Dispose();
        }
        _gameRunning = running;
        return running;
    }

    private static string? ArgumentValue(string[] args, string name)
    {
        int index = Array.FindIndex(args, value =>
            string.Equals(value, name, StringComparison.OrdinalIgnoreCase));
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }

    private static bool OwnerIsAlive(string ownerPath)
    {
        try
        {
            using JsonDocument document = JsonDocument.Parse(File.ReadAllText(ownerPath));
            JsonElement root = document.RootElement;
            int pid = root.GetProperty("pid").GetInt32();
            double createdAt = root.GetProperty("created_at").GetDouble();
            double heartbeat = root.GetProperty("heartbeat").GetDouble();
            double now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            if (pid <= 0 || now - heartbeat > OwnerStaleSeconds)
                return false;
            using Process owner = Process.GetProcessById(pid);
            double actualCreatedAt = new DateTimeOffset(owner.StartTime.ToUniversalTime())
                .ToUnixTimeMilliseconds() / 1000.0;
            return !owner.HasExited && Math.Abs(actualCreatedAt - createdAt) <= 1.0;
        }
        catch
        {
            return false;
        }
    }

    private static void WriteCache(string cachePath, float? cpuTemp, float? cpuClockMhz, float? cpuPower)
    {
        string? directory = Path.GetDirectoryName(cachePath);
        if (!string.IsNullOrWhiteSpace(directory))
            Directory.CreateDirectory(directory);
        string temporary = cachePath + "." + Environment.ProcessId + ".tmp";
        string payload = JsonSerializer.Serialize(new
        {
            cpu_temp = cpuTemp,
            cpu_power = cpuPower,
            cpu_freq = cpuClockMhz.HasValue
                ? (float?)(cpuClockMhz.Value / 1000f)
                : null,
            timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
        });
        try
        {
            File.WriteAllText(temporary, payload);
            File.Move(temporary, cachePath, true);
        }
        finally
        {
            try { File.Delete(temporary); } catch { /* already moved or locked */ }
        }
    }

    private static void AppendErrorLog(string cachePath, Exception exception)
    {
        try
        {
            string logPath = cachePath + ".error.log";
            FileInfo info = new(logPath);
            if (info.Exists && info.Length > 256 * 1024)
                File.Delete(logPath);
            File.AppendAllText(
                logPath,
                DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture)
                + " " + exception.GetType().Name + ": " + exception.Message + Environment.NewLine);
        }
        catch
        {
            // Diagnostics must never create a second failure path.
        }
    }

    [STAThread]
    public static async Task<int> Main(string[] args)
    {
        CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
        string appData = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "CS2Screen");
        string cachePath = ArgumentValue(args, "--cache") ??
            Path.Combine(appData, "elevated-sensors.json");
        string ownerPath = ArgumentValue(args, "--owner-state") ??
            Path.Combine(appData, "instance.json");

        using Mutex singleInstance = new(true, "Local\\SabasakalSensorHost-v3", out bool created);
        if (!created)
            return 0;
        // The UI may start the host a moment before its first heartbeat lands
        // in instance.json (cold start). Exiting at once made the watchdog log
        // "stale sensor cache; direct host restarted" on every launch.
        Stopwatch ownerWait = Stopwatch.StartNew();
        while (!OwnerIsAlive(ownerPath))
        {
            if (ownerWait.ElapsedMilliseconds > 10_000)
                return 0;
            Thread.Sleep(250);
        }

        Computer computer = new()
        {
            IsCpuEnabled = true,
            IsGpuEnabled = false,
            IsMemoryEnabled = false,
            // CPU temperature/clock/power come from the CPU group only. The
            // motherboard group polls SuperIO/EC chips over port I/O, which
            // can add DPC latency spikes while a game runs, so it stays off.
            IsMotherboardEnabled = false,
            IsStorageEnabled = false,
            IsNetworkEnabled = false,
            IsControllerEnabled = false,
            IsPsuEnabled = false,
            IsBatteryEnabled = false,
        };

        try
        {
            try { Process.GetCurrentProcess().PriorityClass = ProcessPriorityClass.BelowNormal; }
            catch { /* keep default priority */ }
            computer.Open();
            UpdateVisitor visitor = new();
            // LibreHardwareMonitor's Update() can stall for seconds while a game
            // saturates the CPU (BelowNormal priority, SMU/MSR contention). In
            // v1.1.3 the cache write sat behind it, the file stopped advancing
            // and the UI watchdog restarted the host ("stale sensor cache").
            // Sampling now runs on its own thread; the writer below publishes
            // the latest values on a fixed 1 s cadence regardless.
            object gate = new();
            float? cpuTemp = null, cpuClock = null, cpuPower = null;
            long lastSampleTicks = Environment.TickCount64;
            using CancellationTokenSource stop = new();
            Thread sampler = new(() =>
            {
                ISensor[] cpu = Array.Empty<ISensor>();
                long sensorsRefreshedAt = long.MinValue;
                while (!stop.IsCancellationRequested)
                {
                    try
                    {
                        computer.Accept(visitor);
                        long now = Environment.TickCount64;
                        if (cpu.Length == 0 || now - sensorsRefreshedAt >= SensorRefreshMilliseconds)
                        {
                            cpu = CpuSensors(computer);
                            sensorsRefreshedAt = now;
                        }
                        float? temp = FirstValue(
                            cpu, SensorType.Temperature,
                            "Tctl/Tdie", "CPU Package", "Core (Tctl/Tdie)",
                            "CPU (Tctl/Tdie)", "CPU", "Core Average");
                        temp ??= AverageValue(
                            cpu, SensorType.Temperature,
                            sensor => sensor.Name.Contains("Core", StringComparison.OrdinalIgnoreCase));
                        float? clock = FirstValue(
                            cpu, SensorType.Clock,
                            "Cores (Average Effective)", "Cores (Average)", "Core #1");
                        clock ??= AverageValue(
                            cpu, SensorType.Clock,
                            sensor => sensor.Name.StartsWith("CPU Core", StringComparison.OrdinalIgnoreCase));
                        float? power = FirstValue(
                            cpu, SensorType.Power, "Package", "CPU Package", "CPU Cores");
                        lock (gate)
                        {
                            cpuTemp = temp; cpuClock = clock; cpuPower = power;
                            lastSampleTicks = Environment.TickCount64;
                        }
                    }
                    catch (Exception sampleError)
                    {
                        AppendErrorLog(cachePath, sampleError);
                    }
                    // 1 s like Task Manager; 2 s while a game runs (performance mode).
                    stop.Token.WaitHandle.WaitOne(GameRunning() ? 2000 : 1000);
                }
            }) { IsBackground = true, Name = "LhmSampler" };
            sampler.Start();
            int consecutiveWriteErrors = 0;
            while (OwnerIsAlive(ownerPath))
            {
                float? temp, clock, power;
                long sampleAge;
                lock (gate)
                {
                    temp = cpuTemp; clock = cpuClock; power = cpuPower;
                    sampleAge = Environment.TickCount64 - lastSampleTicks;
                }
                // A sampler wedged for a full minute is a real hang: exit so the
                // UI starts a fresh host instead of showing frozen values.
                if (sampleAge > 60_000)
                {
                    stop.Cancel();
                    return 2;
                }
                if (sampleAge > 15_000)
                    temp = clock = power = null;
                try
                {
                    WriteCache(cachePath, temp, clock, power);
                    consecutiveWriteErrors = 0;
                }
                catch (Exception writeError) when (
                    writeError is IOException or UnauthorizedAccessException)
                {
                    // A reader (antivirus, the UI, a backup tool) can hold the
                    // cache open without delete sharing for a moment. Keep the
                    // loop alive and retry on the next tick.
                    consecutiveWriteErrors++;
                    if (consecutiveWriteErrors == 1 || consecutiveWriteErrors % 120 == 0)
                        AppendErrorLog(cachePath, writeError);
                }
                await Task.Delay(1000);
            }
            stop.Cancel();
            // Let the sampler finish its current sample before Close() runs.
            sampler.Join(3000);
            return 0;
        }
        catch (Exception exception)
        {
            try
            {
                File.WriteAllText(cachePath + ".error.log", exception.ToString());
            }
            catch
            {
                // Diagnostics must never create a second failure path.
            }
            return 1;
        }
        finally
        {
            computer.Close();
        }
    }
}
