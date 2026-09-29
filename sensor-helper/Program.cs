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

    private static IEnumerable<ISensor> Sensors(Computer computer) =>
        computer.Hardware.SelectMany(Flatten).SelectMany(hardware => hardware.Sensors);

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

    private static void WriteCache(string cachePath, float? cpuTemp, float? cpuClockMhz)
    {
        string? directory = Path.GetDirectoryName(cachePath);
        if (!string.IsNullOrWhiteSpace(directory))
            Directory.CreateDirectory(directory);
        string temporary = cachePath + "." + Environment.ProcessId + ".tmp";
        string payload = JsonSerializer.Serialize(new
        {
            cpu_temp = cpuTemp,
            cpu_freq = cpuClockMhz.HasValue
                ? (float?)(cpuClockMhz.Value / 1000f)
                : null,
            timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
        });
        File.WriteAllText(temporary, payload);
        File.Move(temporary, cachePath, true);
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
        if (!created || !OwnerIsAlive(ownerPath))
            return 0;

        Computer computer = new()
        {
            IsCpuEnabled = true,
            IsGpuEnabled = false,
            IsMemoryEnabled = false,
            IsMotherboardEnabled = true,
            IsStorageEnabled = false,
            IsNetworkEnabled = false,
            IsControllerEnabled = false,
            IsPsuEnabled = false,
            IsBatteryEnabled = false,
        };

        try
        {
            computer.Open();
            UpdateVisitor visitor = new();
            while (OwnerIsAlive(ownerPath))
            {
                computer.Accept(visitor);
                ISensor[] all = Sensors(computer).ToArray();
                ISensor[] cpu = all
                    .Where(sensor => sensor.Hardware.HardwareType == HardwareType.Cpu)
                    .ToArray();

                float? cpuTemp = FirstValue(
                    cpu, SensorType.Temperature,
                    "Tctl/Tdie", "CPU Package", "Core (Tctl/Tdie)",
                    "CPU (Tctl/Tdie)", "CPU", "Core Average");
                cpuTemp ??= AverageValue(
                    cpu, SensorType.Temperature,
                    sensor => sensor.Name.Contains("Core", StringComparison.OrdinalIgnoreCase));

                float? cpuClock = FirstValue(
                    cpu, SensorType.Clock,
                    "Cores (Average Effective)", "Cores (Average)", "Core #1");
                cpuClock ??= AverageValue(
                    cpu, SensorType.Clock,
                    sensor => sensor.Name.StartsWith("CPU Core", StringComparison.OrdinalIgnoreCase));

                WriteCache(cachePath, cpuTemp, cpuClock);
                await Task.Delay(500);
            }
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
