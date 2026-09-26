using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using LibreHardwareMonitor.Hardware;

namespace CS2Screen.SensorBridge;

internal sealed class UpdateVisitor : IVisitor
{
    public void VisitComputer(IComputer computer) => computer.Traverse(this);
    public void VisitHardware(IHardware hardware)
    {
        hardware.Update();
        foreach (IHardware subHardware in hardware.SubHardware)
            subHardware.Accept(this);
    }
    public void VisitSensor(ISensor sensor) { }
    public void VisitParameter(IParameter parameter) { }
}

internal static class Program
{
    private static IEnumerable<IHardware> Flatten(IHardware hardware)
    {
        yield return hardware;
        foreach (IHardware child in hardware.SubHardware)
            foreach (IHardware descendant in Flatten(child))
                yield return descendant;
    }

    private static IEnumerable<ISensor> Sensors(Computer computer) =>
        computer.Hardware.SelectMany(Flatten).SelectMany(hardware => hardware.Sensors);

    private static float? FirstValue(IEnumerable<ISensor> sensors, SensorType type, params string[] names)
    {
        foreach (string name in names)
        {
            ISensor? match = sensors.FirstOrDefault(sensor =>
                sensor.SensorType == type && sensor.Value.HasValue &&
                sensor.Name.Contains(name, StringComparison.OrdinalIgnoreCase));
            if (match?.Value is float value)
                return value;
        }
        return null;
    }

    private static float? AverageValue(IEnumerable<ISensor> sensors, SensorType type, Func<ISensor, bool> filter)
    {
        float[] values = sensors
            .Where(sensor => sensor.SensorType == type && sensor.Value.HasValue && filter(sensor))
            .Select(sensor => sensor.Value!.Value)
            .Where(value => value > 0)
            .ToArray();
        return values.Length == 0 ? null : values.Average();
    }

    public static async Task<int> Main()
    {
        CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
        Computer computer = new()
        {
            IsCpuEnabled = true,
            IsGpuEnabled = true,
            IsMemoryEnabled = false,
            IsMotherboardEnabled = false,
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
            while (true)
            {
                computer.Accept(visitor);
                ISensor[] all = Sensors(computer).ToArray();
                ISensor[] cpu = all.Where(sensor => sensor.Hardware.HardwareType == HardwareType.Cpu).ToArray();
                ISensor[] gpu = all.Where(sensor => sensor.Hardware.HardwareType is HardwareType.GpuNvidia or HardwareType.GpuAmd or HardwareType.GpuIntel).ToArray();

                float? cpuTemp = FirstValue(cpu, SensorType.Temperature, "Tctl/Tdie", "CPU Package", "Core (Tctl/Tdie)", "Core Average");
                cpuTemp ??= AverageValue(cpu, SensorType.Temperature, sensor => sensor.Name.Contains("Core", StringComparison.OrdinalIgnoreCase));

                float? cpuClock = FirstValue(cpu, SensorType.Clock, "Core Average");
                cpuClock ??= AverageValue(cpu, SensorType.Clock, sensor => sensor.Name.StartsWith("CPU Core", StringComparison.OrdinalIgnoreCase));

                float? gpuTemp = FirstValue(gpu, SensorType.Temperature, "GPU Core", "GPU");
                float? gpuClock = FirstValue(gpu, SensorType.Clock, "GPU Core");

                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    cpu_temp = cpuTemp,
                    cpu_freq = cpuClock.HasValue ? cpuClock.Value / 1000f : null,
                    gpu_temp = gpuTemp,
                    gpu_freq = gpuClock.HasValue ? gpuClock.Value / 1000f : null,
                }));
                await Console.Out.FlushAsync();
                await Task.Delay(500);
            }
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine(exception.Message);
            return 1;
        }
        finally
        {
            computer.Close();
        }
    }
}
