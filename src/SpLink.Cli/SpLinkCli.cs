using System.Reflection;
using System.Globalization;
using SpLink.Protocol;
using SpLink.Protocol.Simulation;
using SpLink.Protocol.Transport;
using SpLink.Fronius;

namespace SpLink.Cli;

internal static class SpLinkCli
{
    private const string Usage = """
        splink — cross-platform tools for Selectronic SP PRO inverters

        Usage:
          splink ports                            List serial ports that may be an SP PRO
          splink sites --select-live ...          List the SP PROs visible to your select.live account
          splink probe <connection>               Connect, log in and report link details and clock drift
          splink now <connection> [--watch <s>]   Live data: battery SoC, voltage, current, AC load, charger state
          splink config <connection> [--json] [--all] [--service]
                                                  Read the inverter's configuration (read-only; no write support)
          splink logs <connection>                What logged data the inverter holds (sizes, counts, versions)
          splink read <connection> <addr> <words>
                                          Dump a raw register block (decimal or 0x hex). Read-only.
          splink download <connection> --log <name> [--limit <n>] [--raw]
                                                  Download logged records (newest first). --log: alerts|events|daily|detailed
          splink serve <connection> [--http-port <n>]
                                     [--config <file>] [--fronius name=host,...]
                                     [--idle-timeout <s>] [--cache <s>] [--allow-raw]
                                     [--live-interval <s>] [--today-interval <s>]
                                     [--clock-interval <s>] [--sweep-interval <s>]
                                     [--log-interval <s>] [--fast-cache <s>]
                                                  Serve a read-only JSON API + /metrics. Everything is read
                                                  ON DEMAND: no inverter traffic until a request arrives.
                                                  Also proxies any configured Fronius inverters' Solar API.
          splink clock get <connection>           Read the SP PRO clock
          splink clock set <connection> (--now | --to <yyyy-MM-ddTHH:mm:ss>) [--offset <[+-]hh:mm>]

        Connection (choose one):
          --select-live --sl-user <u> --sl-password <p> [--serial <n>] [--sl-host <host>] [--sl-port <n>]
                                                  Remote, via a select.live unit (default host is the select.live 2 gateway).
                                                  --serial is optional when the account has exactly one SP PRO.
          --port <device|auto> [--baud <rate>]    USB/serial, e.g. /dev/cu.usbserial-XXXX or /dev/ttyACM0.
                                                  'auto' picks the sole candidate port (fails if none or several).
                                                  Auto-baud unless --baud is given.
          --host <name|ip> --tcp-port <n>         SP PRO Ethernet adaptor (serial-to-Ethernet bridge)
          --simulate                              Built-in simulated SP PRO (no hardware needed)

        Options:
          --allow-writes                          Permit writes to the inverter. WITHOUT THIS THE TOOL IS READ-ONLY.
          --allow-raw                             'serve' only: enable /raw, which has no cache and
                                          so can drive unlimited inverter traffic. Local only.
          --password <text>                       SP PRO login password (default "Selectronic SP PRO", or $SPLINK_PASSWORD)
          --timeout <ms>                          Per-request response timeout (default 1000; 5000 via select.live)
          --verbose                               Hex-dump every frame to stderr
          --help

        select.live credentials can also come from $SELECTLIVE_USER/$SELECTLIVE_PASSWORD (or SELECT_LIVE_USERNAME/SELECT_LIVE_PASSWORD),
        including from a .env file in the working directory.
        Exit codes: 0 ok, 1 usage, 2 connection/protocol failure, 3 password rejected, 4 blocked by read-only mode.
        """;

    private static readonly HashSet<string> BooleanFlags = new(StringComparer.OrdinalIgnoreCase)
        { "now", "verbose", "simulate", "select-live", "allow-writes", "allow-raw", "json", "all", "raw", "help", "h", "version", "service" };

    private static string VersionString =>
        typeof(SpLinkCli).Assembly
            .GetCustomAttribute<System.Reflection.AssemblyInformationalVersionAttribute>()?.InformationalVersion
            .Split('+')[0] ?? "unknown";

    private static async Task<int> Main(string[] args)
    {
        LoadDotEnv();
        var options = Parse(args);
        // Packaging needs a way to confirm which build is installed, and every package manager
        // expects --version to work without touching hardware.
        if (options.Has("version") || options.Positional is ["version"])
        {
            Console.WriteLine(VersionString);
            return 0;
        }
        if (options.Has("help") || options.Has("h") || options.Positional.Count == 0)
        {
            Console.WriteLine(Usage);
            return 0;
        }

        using var cts = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };
        // systemd and Docker stop a service with SIGTERM, not Ctrl-C. Left to the runtime's
        // default, SIGTERM ends the process without unwinding, so the SP PRO never gets the
        // disconnect notification and its single session is only freed when the socket drops.
        // Treat it like Ctrl-C so `serve` disposes the session on the way out.
        using var sigterm = System.Runtime.InteropServices.PosixSignalRegistration.Create(
            System.Runtime.InteropServices.PosixSignal.SIGTERM,
            context => { context.Cancel = true; cts.Cancel(); });

        try
        {
            switch (options.Positional[0].ToLowerInvariant())
            {
                case "ports":
                    return Ports();
                case "sites":
                    return await SitesAsync(options, cts.Token);
                case "probe":
                    return await ProbeAsync(options, cts.Token);
                case "now":
                    return await NowAsync(options, cts.Token);
                case "serve":
                    return await ServeAsync(options, cts.Token);
                case "config":
                    return await ConfigAsync(options, cts.Token);
                case "logs":
                    return await LogsAsync(options, cts.Token);
                case "read":
                    return await ReadAsync(options, cts.Token);
                case "download":
                    return await DownloadAsync(options, cts.Token);
                case "clock":
                    if (options.Positional.Count < 2) return UsageError("'clock' needs 'get' or 'set'");
                    return options.Positional[1].ToLowerInvariant() switch
                    {
                        "get" => await ClockGetAsync(options, cts.Token),
                        "set" => await ClockSetAsync(options, cts.Token),
                        var other => UsageError($"unknown clock command '{other}'"),
                    };
                default:
                    return UsageError($"unknown command '{options.Positional[0]}'");
            }
        }
        catch (UsageException ex) { return UsageError(ex.Message); }
        catch (SelectLiveAuthException ex) { Console.Error.WriteLine($"error: {ex.Message}"); return 3; }
        catch (SpProLoginException ex) { Console.Error.WriteLine($"error: {ex.Message}"); return 3; }
        catch (SpProReadOnlyException ex) { Console.Error.WriteLine($"error: {ex.Message}"); return 4; }
        catch (SpProException ex) { Console.Error.WriteLine($"error: {ex.Message}"); return 2; }
        catch (OperationCanceledException) { Console.Error.WriteLine("cancelled"); return 130; }
    }

    private static int Ports()
    {
        var ports = SerialSpProTransport.ListCandidatePorts();
        if (ports.Count == 0)
        {
            Console.WriteLine("No candidate serial ports found.");
        }
        else
        {
            Console.WriteLine("Candidate serial ports:");
            foreach (var p in ports) Console.WriteLine($"  {p}");
        }
        Console.WriteLine();
        Console.WriteLine("If an SP PRO is plugged in but not listed: units with the FTDI interface use a non-standard USB ID (0403:8508).");
        Console.WriteLine("  Linux: sudo modprobe ftdi_sio && echo 0403 8508 | sudo tee /sys/bus/usb-serial/drivers/ftdi_sio/new_id");
        Console.WriteLine("  macOS: the built-in FTDI driver ignores that ID; check 'system_profiler SPUSBDataType' for vendor 0x0403/0x04d8.");
        return 0;
    }

    private static async Task<int> SitesAsync(Options options, CancellationToken ct)
    {
        if (!options.Has("select-live")) throw new UsageException("'sites' needs --select-live with credentials");
        await using var gateway = await OpenSelectLiveAsync(options, TraceFor(options), ct);
        var sites = await gateway.ListSitesAsync(ct);
        PrintSites(sites);
        var devices = await gateway.ListDevicesAsync(ct);
        Console.WriteLine(devices.Count == 0 ? "LIST DEVICES also returned nothing." : $"LIST DEVICES: {string.Join(", ", devices.Select(d => d.Serial))}");
        return 0;
    }

    private static async Task<int> ProbeAsync(Options options, CancellationToken ct)
    {
        await using var client = await ConnectAsync(options, ct);
        try
        {
            PrintClock("SP PRO clock", await client.ReadClockAsync(ct));
            return 0;
        }
        finally
        {
            await client.DisconnectAsync(ct);
        }
    }

    private static async Task<int> LogsAsync(Options options, CancellationToken ct)
    {
        await using var client = await ConnectAsync(options, ct);
        try
        {
            var versions = await client.Logs.ReadFormatVersionsAsync(ct);
            Console.WriteLine($"Record format versions: events {versions.Event}, detailed {versions.Detailed}, daily summary {versions.DailySummary}");
            Console.WriteLine();
            Console.WriteLine($"{"Log",-19} {"entries",8} {"words/entry",11} {"sectors",7} {"per read",8}  current address");
            foreach (var type in Enum.GetValues<SpProLogType>())
            {
                var info = await client.Logs.ReadInfoAsync(type, ct);
                Console.WriteLine($"{type,-19} {info.EntryCount,8} {info.EntrySizeWords,11} {info.SectorCount,7} {info.EntriesPerRead,8}  0x{info.CurrentAddress:X}");
                if (info.SectorCount > 0 && !info.IsEmpty && options.Has("verbose"))
                {
                    var sectors = await client.Logs.ReadSectorTableAsync(info, ct);
                    for (int i = 0; i < sectors.Length / 2; i++)
                        Console.WriteLine($"      sector {i}: 0x{sectors[i * 2]:X}..0x{sectors[i * 2 + 1]:X} ({(sectors[i * 2 + 1] - sectors[i * 2] + 1) / (uint)info.EntrySizeWords} entries)");
                }
            }
            return 0;
        }
        finally
        {
            await client.DisconnectAsync(ct);
        }
    }

    private static async Task<int> DownloadAsync(Options options, CancellationToken ct)
    {
        var type = (options.Get("log") ?? throw new UsageException("--log is required: alerts|events|daily|detailed")).ToLowerInvariant() switch
        {
            "alerts" or "alert" => SpProLogType.AlertEvents,
            "events" or "operational" => SpProLogType.OperationalEvents,
            "daily" or "summary" => SpProLogType.DailySummary,
            "detailed" or "detail" => SpProLogType.Detailed,
            var other => throw new UsageException($"unknown log '{other}': use alerts|events|daily|detailed"),
        };
        int? limit = options.Get("limit") is string l ? int.Parse(l, CultureInfo.InvariantCulture) : null;

        await using var client = await ConnectAsync(options, ct);
        try
        {
            var info = await client.Logs.ReadInfoAsync(type, ct);
            Status($"{type}: {info.EntryCount} entries of {info.EntrySizeWords} words across {info.SectorCount} sectors");
            if (info.IsEmpty)
            {
                Status("nothing logged");
                return 0;
            }

            var sectors = await client.Logs.ReadSectorTableAsync(info, ct);
            client.Logs.Progress = (done, total) => Status($"  {done}/{total} records");
            var records = await client.Logs.ReadAllAsync(info, sectors, limit, ct);
            Status($"downloaded {records.Count} records");

            if (options.Has("raw"))
            {
                foreach (var r in records)
                    Console.WriteLine($"0x{r.Address:X}," + string.Join(",", r.Words));
                return 0;
            }

            var versions = await client.Logs.ReadFormatVersionsAsync(ct);
            var common = await client.ReadScaleFactorsAsync(ct);
            var usable = records.Where(r => !SpProLogDecoder.IsBlankEvent(r) || r.Type is SpProLogType.Detailed or SpProLogType.DailySummary).ToList();
            if (usable.Count != records.Count) Status($"skipped {records.Count - usable.Count} unwritten slots");
            if (usable.Count == 0) return 0;

            var decoded = usable.Select(r => SpProLogDecoder.Decode(r, versions, common)).ToList();
            var columns = decoded[0].Fields.Select(f => f.Unit.Length > 0 ? $"{f.Name} ({f.Unit})" : f.Name).ToList();
            Console.WriteLine("Timestamp," + string.Join(",", columns));
            foreach (var d in decoded)
                Console.WriteLine($"{d.Timestamp:yyyy-MM-dd HH:mm:ss}," +
                    string.Join(",", d.Fields.Select(f => f.Display.Contains(',') ? $"\"{f.Display}\"" : f.Display)));
            return 0;
        }
        finally
        {
            await client.DisconnectAsync(ct);
        }
    }

    private static async Task<int> ConfigAsync(Options options, CancellationToken ct)
    {
        await using var client = await ConnectAsync(options, ct);
        try
        {
            var config = await client.ReadConfigurationAsync(ct);
            // Service Settings sit outside the four configuration blocks, in their own register
            // block, so they need a separate read. Opt-in: it is an extra 127-word transaction and
            // most uses of this command do not want it.
            var service = options.Has("service")
                ? await SpProServiceSettings.ReadAsync(client, ct)
                : [];
            if (config.Unit is { } u && !options.Has("json"))
                Console.WriteLine($"SP PRO {u.Model} ({u.ModelDescription}), serial {u.SerialNumber}, hardware rev {u.HardwareRevision}, {u.BatteryCellCount} battery cells");
            var shown = options.Has("all") ? config.Settings : config.Settings.Where(s => s.IsDecoded).ToList();

            if (options.Has("json"))
            {
                Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(
                    new
                    {
                        settings = shown.Select(s => new { block = s.Block.ToString(), index = s.Index, name = s.Name, converter = s.Converter, raw = s.Raw, decoded = s.Decoded }),
                        // Raw only, deliberately: the map names these but does not say how SP LINK
                        // scales them, and a guessed scaling on a grid-protection value is worse
                        // than a raw count.
                        service_settings = service.Select(s => new { address = s.Address, name = s.Name, raw = s.Raw }),
                    },
                    new System.Text.Json.JsonSerializerOptions { WriteIndented = true, DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.Never }));
                return 0;
            }

            foreach (var group in shown.GroupBy(s => s.Block))
            {
                Console.WriteLine();
                Console.WriteLine($"== {group.Key} ==");
                foreach (var s in group.OrderBy(s => s.Index))
                    Console.WriteLine($"  [{s.Index,3}] {s.Name,-46} {s.Decoded ?? $"raw {s.Raw}",-18} {(s.IsDecoded ? "" : "(" + s.Converter + ")")}");
            }

            if (service.Count > 0)
            {
                Console.WriteLine();
                Console.WriteLine("== Service ==  (raw counts; read-only, several are grid-protection parameters)");
                foreach (var s in service)
                    Console.WriteLine($"  [{s.Address}] {s.Name,-46} raw {s.Raw}");
            }

            var undecoded = config.Settings.Count - config.Settings.Count(s => s.IsDecoded);
            Console.WriteLine();
            Console.WriteLine($"{config.Settings.Count} settings read; {config.Settings.Count - undecoded} decoded, {undecoded} raw-only.");
            if (!options.Has("all") && undecoded > 0)
                Console.WriteLine("Pass --all to include the raw-only settings.");
            if (service.Count == 0)
                Console.WriteLine("Pass --service to also read the Service Settings block.");
            return 0;
        }
        finally
        {
            await client.DisconnectAsync(ct);
        }
    }

    private static async Task<int> ServeAsync(Options options, CancellationToken ct)
    {
        var trace = TraceFor(options);

        // Nothing is read until a request arrives; the session connects lazily and releases the
        // port again once idle, so an unused bridge puts no traffic on the inverter.
        var session = new SpProSession(() => OpenTransportAsync(options, trace, ct), PasswordFor(options))
        {
            Log = line => Console.WriteLine($"[{DateTime.Now:HH:mm:ss}] {line}"),
        };
        // An unrecognised flag used to be accepted and ignored, which meant a mistyped option --
        // or one the usage text advertised but nothing consumed -- looked like it had taken
        // effect. Fail loudly instead. Covers the transport and trace options serve reaches
        // through as well as its own.
        string[] known = [
            // transport
            "port", "baud", "host", "tcp-port", "timeout", "simulate",
            "select-live", "serial", "sl-user", "sl-password", "sl-host", "sl-port", "password",
            // serve
            "http-port", "config", "fronius", "allow-raw", "idle-timeout", "cache",
            "live-interval", "today-interval", "clock-interval", "sweep-interval",
            "log-interval", "fast-cache",
            // general
            "verbose", "help", "h",
        ];
        foreach (var flag in options.Flags.Keys)
            if (!known.Contains(flag, StringComparer.OrdinalIgnoreCase))
                throw new UsageException($"serve: unknown option --{flag}");

        void Seconds(string name, Action<TimeSpan> apply)
        {
            if (options.Get(name) is not string raw) return;
            if (!double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var s) || s < 0)
                throw new UsageException($"serve: --{name} expects a non-negative number of seconds, got '{raw}'");
            apply(TimeSpan.FromSeconds(s));
        }

        Seconds("idle-timeout", v => session.IdleTimeout = v);
        Seconds("cache", v => session.CacheFor = v);
        Seconds("live-interval", v => session.LiveInterval = v);
        Seconds("today-interval", v => session.TodayInterval = v);
        Seconds("clock-interval", v => session.ClockInterval = v);
        Seconds("sweep-interval", v => session.BlockSweepInterval = v);
        Seconds("log-interval", v => session.LogCheckInterval = v);
        Seconds("fast-cache", v => session.FastCacheFor = v);

        IReadOnlyList<FroniusInverterConfig> inverters = [];
        if (options.Get("fronius") is string inline) inverters = BridgeConfig.ParseInline(inline);
        else if (options.Get("config") is string cfg) inverters = BridgeConfig.Load(cfg).ToInverters();
        var fronius = inverters.Count > 0 ? new FroniusService(inverters) : null;

        await using (session)
        await using (fronius)
        {
            var port = options.Get("http-port") is string p ? int.Parse(p, CultureInfo.InvariantCulture) : 8080;
            var server = new JsonServer(session, port, fronius, allowRaw: options.Has("allow-raw"));
            if (fronius is not null)
                Console.WriteLine($"Fronius (on demand): {string.Join(", ", inverters.Select(i => $"{i.Name}@{i.Host}"))}");
            Console.WriteLine($"On-demand: no inverter traffic until a request arrives. "
                              + $"SP PRO link released after {session.IdleTimeout.TotalSeconds:0}s idle.");
            Console.WriteLine("Read-only — no writes are possible from this process. Press Ctrl-C to stop.");

            // Either task failing must stop the other, so a port clash surfaces immediately.
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct);
            var serving = server.RunAsync(linked.Token);
            var reaping = session.RunIdleReaperAsync(linked.Token);
            var finished = await Task.WhenAny(serving, reaping);
            await linked.CancelAsync();
            try { await finished; }
            catch (OperationCanceledException) { }
            try { await Task.WhenAll(serving, reaping); }
            catch (OperationCanceledException) { }
        }
        return 0;
    }

    private static string PasswordFor(Options options)
        => options.Get("password") ?? Env("SPLINK_PASSWORD") ?? SpProLogin.DefaultPassword;

    private static async Task<int> NowAsync(Options options, CancellationToken ct)
    {
        await using var client = await ConnectAsync(options, ct);
        try
        {
            var interval = options.Get("watch") is string w ? TimeSpan.FromSeconds(double.Parse(w, CultureInfo.InvariantCulture)) : TimeSpan.Zero;
            var unit = await client.ReadUnitInfoAsync(ct);
            Status($"SP PRO {unit.Model} ({unit.ModelDescription}), serial {unit.SerialNumber}");
            var scale = await client.ReadScaleFactorsAsync(ct);
            if (options.Has("verbose"))
                Console.Error.WriteLine($"  scale factors: acV={scale.AcVolts} acA={scale.AcCurrent} dcV={scale.DcVolts} dcA={scale.DcCurrent} temp={scale.Temperature}");

            while (true)
            {
                PrintLive(await client.ReadLiveAsync(ct), options.Has("verbose"));
                if (interval <= TimeSpan.Zero) break;
                Console.WriteLine();
                await Task.Delay(interval, ct);
            }
            return 0;
        }
        finally
        {
            await client.DisconnectAsync(ct);
        }
    }

    private static void PrintLive(SpProLiveReading r, bool verbose)
    {
        Console.WriteLine($"[{DateTime.Now:HH:mm:ss}]");
        Console.WriteLine($"  Battery SoC   {(r.BatterySoCPercent is double soc ? $"{soc,8:F1} %" : "   disabled")}");
        Console.WriteLine($"  Battery       {r.BatteryVolts,8:F2} V   {r.BatteryAmps,8:F2} A   {r.BatteryKilowatts,8:F3} kW  ({(r.BatteryAmps >= 0 ? "charging" : "discharging")})");
        Console.WriteLine($"  DC current    {r.DcAmps,8:F2} A");
        Console.WriteLine($"  AC load       {r.AcLoadKilowatts,8:F3} kW");
        Console.WriteLine($"  AC output     {r.AcVolts,8:F1} V   {r.AcFrequencyHz,8:F2} Hz");
        Console.WriteLine($"  Charger       {r.ChargerState}");
        Console.WriteLine($"  Generator     {r.GeneratorState} ({r.GeneratorReason})");
        if (verbose)
            Console.WriteLine($"  (legacy AC-load interpretation: {r.AcLoadKilowattsLegacy:F3} kW)");
    }

    /// <summary>
    /// Dumps an arbitrary register block. SP LINK reads around fifty named blocks and this tool
    /// decodes a handful, so this exists to see what a block actually holds before deciding
    /// whether it is worth decoding. Read-only: there is deliberately no matching write.
    /// </summary>
    private static async Task<int> ReadAsync(Options options, CancellationToken ct)
    {
        if (options.Positional.Count < 3)
            throw new UsageException("'read' needs an address and a word count");
        var address = ParseAddress(options.Positional[1]);
        if (!int.TryParse(options.Positional[2], out var count) || count is < 1 or > 256)
            throw new UsageException("word count must be 1..256");

        await using var client = await ConnectAsync(options, ct);
        try
        {
            var words = await client.ReadWordsAsync(address, count, ct);
            Console.WriteLine($"{address} (0x{address:X}) x {words.Length} words");
            for (var i = 0; i < words.Length; i += 8)
            {
                var row = words.Skip(i).Take(8).ToArray();
                var hex = string.Join(" ", row.Select(x => x.ToString("X4")));
                var dec = string.Join(" ", row.Select(x => x.ToString().PadLeft(6)));
                Console.WriteLine($"  [{i,3}] {hex,-39} {dec}");
            }
            return 0;
        }
        finally
        {
            await client.DisconnectAsync(ct);
        }
    }

    private static uint ParseAddress(string text) =>
        text.StartsWith("0x", StringComparison.OrdinalIgnoreCase)
            ? Convert.ToUInt32(text[2..], 16)
            : uint.TryParse(text, out var n) ? n
            : throw new UsageException($"cannot parse address '{text}'");

    private static async Task<int> ClockGetAsync(Options options, CancellationToken ct)
    {
        await using var client = await ConnectAsync(options, ct);
        try
        {
            PrintClock("SP PRO clock", await client.ReadClockAsync(ct));
            return 0;
        }
        finally
        {
            await client.DisconnectAsync(ct);
        }
    }

    private static async Task<int> ClockSetAsync(Options options, CancellationToken ct)
    {
        if (!options.Has("allow-writes"))
            throw new UsageException("'clock set' writes to the inverter; re-run with --allow-writes to confirm");

        DateTime target;
        if (options.Has("now"))
            target = DateTime.Now;
        else if (options.Get("to") is string to)
            target = DateTime.Parse(to, CultureInfo.InvariantCulture, DateTimeStyles.AssumeLocal | DateTimeStyles.AllowWhiteSpaces);
        else
            throw new UsageException("clock set needs --now or --to <yyyy-MM-ddTHH:mm:ss>");

        if (options.Get("offset") is string offset)
            target += ParseOffset(offset);

        await using var client = await ConnectAsync(options, ct);
        try
        {
            PrintClock("Before", await client.ReadClockAsync(ct));
            // Encode at the last moment so the seconds we write are as fresh as possible.
            if (options.Has("now") && options.Get("offset") is null) target = DateTime.Now;
            Console.WriteLine($"Setting SP PRO clock to {target:yyyy-MM-dd HH:mm:ss} ({target.DayOfWeek}) ...");
            await client.SetClockAsync(target, ct);
            PrintClock("After", await client.ReadClockAsync(ct));
            return 0;
        }
        finally
        {
            await client.DisconnectAsync(ct);
        }
    }

    private static async Task<SpProClient> ConnectAsync(Options options, CancellationToken ct)
    {
        var trace = TraceFor(options);
        var transport = await OpenTransportAsync(options, trace, ct);
        var client = new SpProClient(transport, allowWrites: options.Has("allow-writes")) { Trace = trace };
        if (options.Get("timeout") is string timeout)
            client.Channel.ResponseTimeout = TimeSpan.FromMilliseconds(int.Parse(timeout, CultureInfo.InvariantCulture));
        else if (transport is SelectLiveTransport)
            client.Channel.ResponseTimeout = TimeSpan.FromSeconds(5); // SP LINK allows far longer round trips via the gateway

        var password = options.Get("password")
                       ?? Environment.GetEnvironmentVariable("SPLINK_PASSWORD")
                       ?? SpProLogin.DefaultPassword;
        try
        {
            Status($"Connected via {transport.Description}; detecting SP PRO ...");
            var info = await client.ConnectAsync(password, ct);
            Status($"SP PRO link port {info.LinkPort}, configured for {info.BaudDescription} baud, {(info.LoggedIn ? "login accepted" : "no login required")}");
            return client;
        }
        catch
        {
            await client.DisposeAsync();
            throw;
        }
    }

    private static async Task<ISpProTransport> OpenTransportAsync(Options options, Action<string>? trace, CancellationToken ct)
    {
        if (options.Has("simulate"))
        {
            // Start the simulated unit 7½ minutes slow so 'probe' and 'clock set' have something to show.
            return new FakeSpPro(DateTime.Now.AddMinutes(-7).AddSeconds(-30));
        }
        if (options.Has("select-live"))
        {
            var gateway = await OpenSelectLiveAsync(options, trace, ct);
            try
            {
                var serial = options.Get("serial");
                if (serial is null)
                {
                    var sites = await gateway.ListSitesAsync(ct);
                    if (sites.Count == 1)
                    {
                        serial = sites[0].Serial;
                        Status($"Using the only SP PRO on this account: {serial} ({sites[0].Name})");
                    }
                    else
                    {
                        PrintSites(sites);
                        throw new UsageException(sites.Count == 0
                            ? "no SP PROs are linked to this select.live account"
                            : "several SP PROs are available; choose one with --serial <n>");
                    }
                }
                Status($"Asking select.live to connect to SP PRO {serial} ...");
                await gateway.SelectDeviceAsync(serial, ct);
                return gateway;
            }
            catch
            {
                await gateway.DisposeAsync();
                throw;
            }
        }
        if (options.Get("host") is string host)
        {
            if (options.Get("tcp-port") is not string tcpPort)
                throw new UsageException("--host needs --tcp-port <n> (the adaptor's serial port number)");
            Status($"Connecting to {host}:{tcpPort} ...");
            return await TcpSpProTransport.ConnectAsync(host, int.Parse(tcpPort, CultureInfo.InvariantCulture), ct);
        }
        if (options.Get("port") is string device)
        {
            if (string.Equals(device, "auto", StringComparison.OrdinalIgnoreCase))
            {
                // Device numbering can shift across reboots, so resolve by discovery rather than a fixed node.
                var candidates = SerialSpProTransport.ListCandidatePorts();
                device = candidates.Count switch
                {
                    1 => candidates[0],
                    0 => throw new SpProException("--port auto found no candidate serial ports; is the SP PRO plugged in?"),
                    _ => throw new SpProException(
                        $"--port auto found {candidates.Count} candidate ports ({string.Join(", ", candidates)}); name one explicitly"),
                };
                Status($"auto-selected {device}");
            }

            if (options.Get("baud") is string baud)
                return SerialSpProTransport.Open(device, int.Parse(baud, CultureInfo.InvariantCulture));
            Status($"Scanning {device} for an SP PRO (auto-baud) ...");
            return await SerialSpProTransport.OpenWithAutoBaudAsync(device, null, trace, ct);
        }
        throw new UsageException("specify --select-live, --port <device>, --host <name> --tcp-port <n>, or --simulate");
    }

    private static async Task<SelectLiveTransport> OpenSelectLiveAsync(Options options, Action<string>? trace, CancellationToken ct)
    {
        var user = options.Get("sl-user") ?? Env("SELECTLIVE_USER", "SELECT_LIVE_USERNAME")
                   ?? throw new UsageException("--select-live needs --sl-user (or $SELECTLIVE_USER)");
        var password = options.Get("sl-password") ?? Env("SELECTLIVE_PASSWORD", "SELECT_LIVE_PASSWORD")
                       ?? throw new UsageException("--select-live needs --sl-password (or $SELECTLIVE_PASSWORD)");
        var host = options.Get("sl-host") ?? SelectLiveTransport.SelectLive2Host;
        var port = options.Get("sl-port") is string p ? int.Parse(p, CultureInfo.InvariantCulture) : SelectLiveTransport.DefaultPort;

        Status($"Logging in to select.live at {host}:{port} as {user} ...");
        return await SelectLiveTransport.ConnectAsync(user, password, host, port, trace, ct);
    }

    private static void PrintSites(IReadOnlyList<SelectLiveSite> sites)
    {
        if (sites.Count == 0)
        {
            Console.WriteLine("No SP PROs are linked to this select.live account.");
            return;
        }
        Console.WriteLine("SP PROs on this select.live account:");
        foreach (var site in sites) Console.WriteLine($"  {site.Serial,-12} {site.Name}");
    }

    private static string? Env(params string[] names)
        => names.Select(Environment.GetEnvironmentVariable).FirstOrDefault(v => !string.IsNullOrEmpty(v));

    /// <summary>Loads KEY=VALUE lines from a .env file in the working directory without overriding real environment variables.</summary>
    private static void LoadDotEnv()
    {
        const string path = ".env";
        if (!File.Exists(path)) return;
        foreach (var raw in File.ReadLines(path))
        {
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith('#')) continue;
            if (line.StartsWith("export ", StringComparison.Ordinal)) line = line[7..];
            int eq = line.IndexOf('=');
            if (eq <= 0) continue;
            var key = line[..eq].Trim();
            var value = line[(eq + 1)..].Trim().Trim('"', '\'');
            if (Environment.GetEnvironmentVariable(key) is null) Environment.SetEnvironmentVariable(key, value);
        }
    }

    /// <summary>Progress and diagnostics go to stderr so stdout carries only data.</summary>
    private static void Status(string message) => Console.Error.WriteLine(message);

    private static Action<string>? TraceFor(Options options)
        => options.Has("verbose") ? line => Console.Error.WriteLine("  " + line) : null;

    private static void PrintClock(string label, SpProClockReading reading)
    {
        var drift = reading.Time - DateTime.Now;
        Console.WriteLine($"{label}: {reading.Time:yyyy-MM-dd HH:mm:ss.fff} ({reading.Time.DayOfWeek}) — {FormatDrift(drift)}");
        if (!reading.DayOfWeekConsistent)
            Console.WriteLine($"  note: the SP PRO's stored weekday is {reading.StoredDayOfWeek}, which does not match the date");
    }

    private static string FormatDrift(TimeSpan drift)
    {
        var abs = drift.Duration();
        if (abs < TimeSpan.FromSeconds(2)) return "in sync with this computer";
        string magnitude = abs.TotalHours >= 1 ? $"{(int)abs.TotalHours}h {abs.Minutes}m {abs.Seconds}s"
                         : abs.TotalMinutes >= 1 ? $"{abs.Minutes}m {abs.Seconds}s"
                         : $"{abs.Seconds}s";
        return drift > TimeSpan.Zero ? $"{magnitude} ahead of this computer" : $"{magnitude} behind this computer";
    }

    private static TimeSpan ParseOffset(string text)
    {
        bool negative = text.StartsWith('-');
        var span = TimeSpan.ParseExact(text.TrimStart('+', '-'), @"h\:mm", CultureInfo.InvariantCulture);
        return negative ? -span : span;
    }

    private static int UsageError(string message)
    {
        Console.Error.WriteLine($"error: {message}");
        Console.Error.WriteLine("Run 'splink --help' for usage.");
        return 1;
    }

    private static Options Parse(string[] args)
    {
        var options = new Options();
        for (int i = 0; i < args.Length; i++)
        {
            var arg = args[i];
            if (arg.StartsWith("--", StringComparison.Ordinal))
            {
                var name = arg[2..];
                string? value = null;
                if (!BooleanFlags.Contains(name) && i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
                    value = args[++i];
                options.Flags[name] = value;
            }
            else
            {
                options.Positional.Add(arg);
            }
        }
        return options;
    }

    private sealed class Options
    {
        public List<string> Positional { get; } = new();
        public Dictionary<string, string?> Flags { get; } = new(StringComparer.OrdinalIgnoreCase);
        public bool Has(string name) => Flags.ContainsKey(name);
        public string? Get(string name) => Flags.TryGetValue(name, out var value) ? value : null;
    }

    private sealed class UsageException(string message) : Exception(message);
}
