// CadBridge: lets an external process (the cadbridge MCP server) edit the drawing open in AutoCAD 2025+ or
// an OEM product built on it (CAD&PILLAR 11, ...), live, as DWG. It needs no COM: some OEMs (CAD&PILLAR)
// disable the ActiveX AcadApplication entirely (every call returns E_FAIL, even in-process).
// Loaded at CAD startup (registry demand-load, see installer.ps1), it serves JSON requests on the named pipe
// \\.\pipe\cadbridge-<exe>-<pid> (current Windows user only; one pipe per running CAD, so several CADs can run).
// Each request runs on CAD's main thread through the managed API, directly on the open DWG.
//
// Protocol: one JSON object per line -> one JSON line back: {"ok":true,"result":...} | {"ok":false,"error":"..."}
// Request: {"op": "...", ...}  or  {"op":"batch","ops":[{...},...]}  (batch = one main-thread call, one undo-able tx)
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;
using J = System.Collections.Generic.Dictionary<string, object>;

[assembly: ExtensionApplication(typeof(CadBridge.Bridge))]
[assembly: CommandClass(typeof(CadBridge.Bridge))]

namespace CadBridge
{
    public class Bridge : IExtensionApplication
    {
        static readonly System.Diagnostics.Process Me = System.Diagnostics.Process.GetCurrentProcess();
        static readonly string PipeName = "cadbridge-" + Me.ProcessName.ToLowerInvariant() + "-" + Me.Id;
        static readonly string LogPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "CadBridge", "cadbridge.log");

        static void Log(string s)
        {
            try { File.AppendAllText(LogPath, DateTime.Now.ToString("s") + " " + s + Environment.NewLine); } catch { }
        }

        // Pipe requests are marshalled onto CAD's main (UI) thread through its WPF dispatcher,
        // captured in Initialize, which AutoCAD calls on the main thread.
        static System.Windows.Threading.Dispatcher _disp;

        public void Initialize()
        {
            _disp = System.Windows.Threading.Dispatcher.CurrentDispatcher;
            Log("Initialize on thread " + Environment.CurrentManagedThreadId);
            StartServer();
        }

        static void RunOnMain(Action a) => _disp.BeginInvoke(a);

        public void Terminate() { }

        [CommandMethod("CADBRIDGE")]
        public void Status()
        {
            AcApp.DocumentManager.MdiActiveDocument?.Editor.WriteMessage("\nCadBridge listening on \\\\.\\pipe\\" + PipeName + "\n");
        }

        // ---------------- pipe server ----------------
        static void StartServer()
        {
            var t = new Thread(ServerLoop) { IsBackground = true, Name = "CadBridgePipe" };
            t.Start();
        }

        static void ServerLoop()
        {
            Log("pipe server started on \\\\.\\pipe\\" + PipeName);
            while (true)
            {
                try
                {
                    // Several instances: the next listener exists before this client is served, so a
                    // client never finds the pipe missing. CAD work itself stays serialised on the dispatcher.
#if NETFRAMEWORK
                    // .NET Framework (AutoCAD 2021-2024): restrict the pipe to the current Windows user with an ACL.
                    var acl = new PipeSecurity();
                    acl.AddAccessRule(new PipeAccessRule(System.Security.Principal.WindowsIdentity.GetCurrent().User,
                        PipeAccessRights.FullControl, System.Security.AccessControl.AccessControlType.Allow));
                    var pipe = new NamedPipeServerStream(PipeName, PipeDirection.InOut, NamedPipeServerStream.MaxAllowedServerInstances,
                        PipeTransmissionMode.Byte, PipeOptions.None, 0, 0, acl);
#else
                    var pipe = new NamedPipeServerStream(PipeName, PipeDirection.InOut,
                        NamedPipeServerStream.MaxAllowedServerInstances, PipeTransmissionMode.Byte, PipeOptions.CurrentUserOnly);
#endif
                    pipe.WaitForConnection();
                    new Thread(() => Serve(pipe)) { IsBackground = true }.Start();
                }
                catch (System.Exception ex) { Log("pipe error: " + ex.Message); Thread.Sleep(200); }
            }
        }

        static void Serve(NamedPipeServerStream pipe)
        {
            int served = 0;
            try
            {
                using (pipe)
                {
                    var reader = new StreamReader(pipe, new UTF8Encoding(false));
                    var writer = new StreamWriter(pipe, new UTF8Encoding(false));
                    string line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        writer.Write(Handle(line) + "\n");
                        writer.Flush();
                        served++;
                    }
                }
            }
            catch (System.Exception ex) { Log("serve error after " + served + " requests: " + ex.Message); }
            if (served == 0) Log("connection closed without a request");
        }

        static string Handle(string line)
        {
            try
            {
                var r = (J)MiniJson.Parse(line);
                string op = Str(r, "op", "");
                // Command-line jobs wait on this pipe thread, polling CAD, so the main thread stays free while they run.
                object res = (op == "command" && !Bool(r, "async", false)) || op == "lisp" || op == "cancel"
                    ? Job(op, r)
                    : OnMain(() => Dispatch(r), 120); // ponytail: fixed 120 s; a modal dialog blocks the main thread
                return MiniJson.Write(new Dictionary<string, object> { ["ok"] = true, ["result"] = res });
            }
            catch (System.Exception ex)
            {
                string msg = ex is TimeoutException ? "timeout: " + ex.Message : ex.GetType().Name + ": " + ex.Message;
                return MiniJson.Write(new Dictionary<string, object> { ["ok"] = false, ["error"] = msg });
            }
        }

        // Runs f on CAD's main thread and waits for it; its exception is rethrown here.
        static T OnMain<T>(Func<T> f, double seconds)
        {
            var tcs = new TaskCompletionSource<T>();
            RunOnMain(() => { try { tcs.SetResult(f()); } catch (System.Exception ex) { tcs.SetException(ex); } });
            bool done;
            try { done = tcs.Task.Wait(TimeSpan.FromSeconds(seconds)); }
            catch (AggregateException ae) { throw ae.InnerException; }
            if (!done) throw new TimeoutException("CAD main thread busy (modal dialog, or LISP still running?)");
            return tcs.Task.Result;
        }

        // ---------------- synchronous command-line jobs (pipe thread) ----------------
        // command / lisp: queue the text on the command line, then poll CAD until it is idle again. A job that is
        // still inside a command at its timeout (waiting for input it never got) is cancelled with ESC ESC.
        static readonly object JobLock = new object();
        static long _jobSeq;

        static object Job(string op, J r)
        {
            int timeout = Math.Min(Math.Max(Int(r, "timeout_ms", 30000), 100), 600000);
            if (!Monitor.TryEnter(JobLock, Int(r, "queue_wait_ms", 10000)))
                return new J { ["status"] = "busy", ["reason"] = "another CadBridge command is still running" };
            try
            {
                if (op == "cancel") return Cancel();
                long id = Interlocked.Increment(ref _jobSeq);
                string retFile = op == "lisp" ? Path.Combine(Path.GetTempPath(), "cadbridge-" + Me.Id + "-" + id + ".json") : null;
                string text = op == "lisp" ? LispWrap(Str(r, "expr"), retFile) : Str(r, "text").TrimEnd('\n') + "\n";
                if (op == "lisp" || text.TrimStart().StartsWith("(")) text = ShortLines(text, 200);
                var started = new int[1];
                CommandEventHandler onCmd = (s, e) => Interlocked.Increment(ref started[0]);
                LispWillStartEventHandler onLisp = (s, e) => Interlocked.Increment(ref started[0]);
                Document doc = null;
                long seed = 0;
                var waited = System.Diagnostics.Stopwatch.StartNew();
                J busy;
                while (true)
                {
                    busy = OnMain(() =>
                    {
                        doc = Doc();
                        if (CmdActive() != 0) return Busy();
                        seed = doc.Database.Handseed.Value;
                        doc.CommandWillStart += onCmd;
                        doc.LispWillStart += onLisp;
                        doc.SendStringToExecute(text, true, false, false);
                        return null;
                    }, 30);
                    // Active without a command name: a LISP or script is running (CAD&PILLAR runs its own, CDLoadMenu,
                    // in every new drawing). Wait for it; an open command (the user's) answers busy at once.
                    if (busy == null || !string.IsNullOrEmpty(busy["command"] as string) || waited.ElapsedMilliseconds > Int(r, "queue_wait_ms", 10000)) break;
                    Thread.Sleep(200);
                }
                if (busy != null) return busy;

                var sw = System.Diagnostics.Stopwatch.StartNew();
                string status = null, note = null;
                int idle = 0;
                try
                {
                    while (status == null)
                    {
                        Thread.Sleep(100);
                        int active;
                        try { active = OnMain<int>(CmdActive, Math.Max(2, (timeout - sw.ElapsedMilliseconds) / 1000.0 + 2)); }
                        catch (TimeoutException) { status = "timeout"; note = "CAD main thread blocked (endless LISP loop or modal dialog): press ESC in the CAD"; break; }
                        bool begun = started[0] > 0 || (retFile != null && File.Exists(retFile)) || sw.ElapsedMilliseconds > 3000;
                        // ponytail: idle twice 100 ms apart = finished; a script with long pauses between commands could end early
                        idle = active == 0 && begun ? idle + 1 : 0;
                        if (idle >= 2) status = "done";
                        else if (sw.ElapsedMilliseconds > timeout)
                        {
                            status = "timeout";
                            if (active != 0 && Str(r, "on_timeout", "cancel") == "cancel")
                            {
                                note = "still in " + OnMain(() => Sv("CMDNAMES"), 10) + " at '" + OnMain(() => Sv("LASTPROMPT"), 10) + "': cancelled";
                                status = CancelNow() ? "cancelled" : "timeout";
                            }
                        }
                    }
                }
                finally
                {
                    try { OnMain(() => { doc.CommandWillStart -= onCmd; doc.LispWillStart -= onLisp; return 0; }, 10); } catch { }
                }

                var res = new J { ["status"] = status, ["ms"] = sw.ElapsedMilliseconds };
                if (note != null) res["note"] = note;
                try
                {
                    OnMain(() =>
                    {
                        res["command"] = Sv("CMDNAMES");
                        res["prompt"] = Sv("LASTPROMPT");
                        res["created"] = Created(doc.Database, seed);
                        return 0;
                    }, 10);
                }
                catch (TimeoutException) { }
                if (op == "lisp")
                {
                    if (File.Exists(retFile))
                    {
                        try
                        {
                            var o = (J)MiniJson.Parse(File.ReadAllText(retFile, Encoding.UTF8).Trim());
                            if (o.ContainsKey("error")) { res["status"] = "error"; res["error"] = o["error"]; }
                            else res["value"] = o["value"];
                        }
                        catch (System.Exception ex) { res["status"] = "error"; res["error"] = "unreadable LISP result: " + ex.Message; }
                        finally { try { File.Delete(retFile); } catch { } }
                    }
                    else if (status == "done") { res["status"] = "error"; res["error"] = "no value returned: " + res["prompt"]; }
                }
                return res;
            }
            finally { Monitor.Exit(JobLock); }
        }

        static int CmdActive() => Convert.ToInt32(AcApp.GetSystemVariable("CMDACTIVE"));

        static string Sv(string name)
        {
            try { return AcApp.GetSystemVariable(name)?.ToString(); } catch { return null; }
        }

        static J Busy() => new J
        {
            ["status"] = "busy", ["command"] = Sv("CMDNAMES"), ["prompt"] = Sv("LASTPROMPT"),
            ["hint"] = "the CAD is inside a command (the user's, or one left open): cad_cancel, or ask the user to finish it"
        };

        static object Cancel()
        {
            var names = OnMain(() => CmdActive() == 0 ? null : Sv("CMDNAMES") ?? "?", 30);
            if (names == null) return new J { ["status"] = "idle", ["cancelled"] = null };
            return new J { ["status"] = CancelNow() ? "cancelled" : "still_busy", ["cancelled"] = names };
        }

        // ESC ESC on the command line, then wait until no command is active (two tries, about 3 s each).
        static bool CancelNow()
        {
            for (int attempt = 0; attempt < 2; attempt++)
            {
                OnMain(() => { Doc().SendStringToExecute("\x03\x03", true, false, false); return 0; }, 10);
                for (int i = 0; i < 30; i++)
                {
                    Thread.Sleep(100);
                    if (OnMain<int>(CmdActive, 10) == 0) return true;
                }
            }
            return false;
        }

        // Handles of the model-space objects created since the job started (handles are never reused).
        static object Created(Database db, long seed)
        {
            var hs = new List<string>();
            int n = 0;
            using (var tr = db.TransactionManager.StartOpenCloseTransaction())
                foreach (ObjectId id in Ms(tr, db, OpenMode.ForRead))
                    if (id.Handle.Value >= seed && ++n <= 200) hs.Add(id.Handle.ToString());
            return new J { ["count"] = n, ["handles"] = hs };
        }

        // The expression runs inside vl-catch-all-apply; the LISP encodes its value (or error message) as JSON into a
        // temp file the job reads back. Plain LISP only: CAD&PILLAR does not register the plugin's .NET LISP functions.
        // JSON: lists -> arrays (a dotted pair -> [car, cdr]), enames and selection sets -> handles, nil -> null, T -> true.
        // ponytail: strcat per character, fine for results up to a few hundred KB
        const string LispJson =
            "(defun cadbridge:esc (s / o) (setq o \"\") (foreach c (vl-string->list s) (setq o (strcat o (cond ((= c 34) \"\\\\\\\"\") " +
            "((= c 92) \"\\\\\\\\\") ((= c 10) \"\\\\n\") ((= c 13) \"\\\\r\") ((= c 9) \"\\\\t\") ((< c 32) \"\") (T (chr c)))))) o)" +
            "(defun cadbridge:join (l) (if l (apply 'strcat (cons (car l) (mapcar '(lambda (x) (strcat \",\" x)) (cdr l)))) \"\"))" +
            "(defun cadbridge:j (v / i l) (cond ((null v) \"null\") ((eq v T) \"true\") ((= (type v) 'INT) (itoa v)) ((= (type v) 'REAL) (rtos v 2 15))" +
            " ((= (type v) 'STR) (strcat \"\\\"\" (cadbridge:esc v) \"\\\"\"))" +
            " ((= (type v) 'ENAME) (strcat \"\\\"\" (vl-princ-to-string (cdr (assoc 5 (entget v)))) \"\\\"\"))" +
            " ((= (type v) 'PICKSET) (setq i (sslength v)) (while (> i 0) (setq i (1- i) l (cons (ssname v i) l))) (cadbridge:j l))" +
            " ((= (type v) 'LIST) (if (vl-list-length v) (strcat \"[\" (cadbridge:join (mapcar 'cadbridge:j v)) \"]\")" +
            " (strcat \"[\" (cadbridge:j (car v)) \",\" (cadbridge:j (cdr v)) \"]\")))" +
            " (T (strcat \"\\\"\" (cadbridge:esc (vl-princ-to-string v)) \"\\\"\"))))";

        // CAD&PILLAR's LISP reader crashes on one long input line (measured: 1,021 characters -> access violation, then
        // the whole process; the same text in 134-character lines works). Break LISP text at spaces outside strings and
        // comments, where a newline means the same as a space. A single string literal longer than max stays one line.
        static string ShortLines(string s, int max)
        {
            var b = new StringBuilder(s.Length + s.Length / max + 1);
            bool str = false, comment = false;
            int col = 0;
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                if (str)
                {
                    if (c == '\\' && i + 1 < s.Length) { b.Append(c).Append(s[++i]); col += 2; continue; }
                    if (c == '"') str = false;
                }
                else if (comment) { if (c == '\n') comment = false; }
                else if (c == '"') str = true;
                else if (c == ';') comment = true;
                else if (c == ' ' && col >= max) { b.Append('\n'); col = 0; continue; }
                b.Append(c);
                col = c == '\n' ? 0 : col + 1;
            }
            return b.ToString();
        }

        static string LispWrap(string expr, string retFile) =>
            "(progn " + LispJson + " (setq cadbridge:r (vl-catch-all-apply '(lambda () " + expr + "\n)))" +
            " (setq cadbridge:f (open \"" + retFile.Replace('\\', '/') + "\" \"w\" \"utf8\"))" +
            " (write-line (if (vl-catch-all-error-p cadbridge:r) (strcat \"{\\\"error\\\":\" (cadbridge:j (vl-catch-all-error-message cadbridge:r)) \"}\")" +
            " (strcat \"{\\\"value\\\":\" (cadbridge:j cadbridge:r) \"}\")) cadbridge:f) (close cadbridge:f) (princ))\n";

        // ---------------- operations (main thread) ----------------
        static object Dispatch(J r)
        {
            string op = Str(r, "op");
            switch (op)
            {
                case "ping":
                    return new
                    {
                        pong = true, version = typeof(Bridge).Assembly.GetName().Version?.ToString(3), exe = Me.ProcessName, pid = Me.Id, path = Me.MainModule?.FileName,
                        acadver = AcApp.GetSystemVariable("ACADVER")?.ToString(), product = AcApp.GetSystemVariable("PRODUCT")?.ToString()
                    };
                case "docs": return AcApp.DocumentManager.Cast<Document>().Select(d => d.Name).ToList();
                case "open":
                    {
                        string path = Path.GetFullPath(Str(r, "path"));
                        // Already open: just activate it (Open() on an open drawing fails with eInvalidContext).
                        var d = AcApp.DocumentManager.Cast<Document>().FirstOrDefault(x =>
                                    string.Equals(Path.GetFullPath(x.Name), path, StringComparison.OrdinalIgnoreCase))
                                ?? AcApp.DocumentManager.Open(path, Bool(r, "read_only", false));
                        AcApp.DocumentManager.MdiActiveDocument = d;
                        return d.Name;
                    }
                case "new":
                    {
                        var d = AcApp.DocumentManager.Add(Str(r, "template", ""));
                        AcApp.DocumentManager.MdiActiveDocument = d;
                        return d.Name;
                    }
                case "command":
                    {
                        // "async": true only: queued, not waited for (the default, synchronous form is Job).
                        Doc().SendStringToExecute(Str(r, "text").TrimEnd('\n') + "\n", true, false, false);
                        return "queued";
                    }
            }
            var doc = Doc();
            if (op == "save")
            {
                string path = Str(r, "path", doc.Name);
                using (doc.LockDocument())
                    doc.Database.SaveAs(path, true, DwgVersion.Current, doc.Database.SecurityParameters);
                return path;
            }
            using (doc.LockDocument())
            using (var tr = doc.Database.TransactionManager.StartTransaction())
            {
                object res;
                if (op == "batch")
                {
                    var outs = new List<object>();
                    int i = 0;
                    foreach (J sub in Arr(r, "ops"))
                    {
                        try { outs.Add(Op(doc, tr, sub)); }
                        catch (System.Exception ex) { throw new InvalidOperationException("op #" + i + " (" + Str(sub, "op") + "): " + ex.Message, ex); }
                        i++;
                    }
                    res = outs;
                }
                else res = Op(doc, tr, r);
                tr.Commit();
                return res;
            }
        }

        static Document Doc()
        {
            return AcApp.DocumentManager.MdiActiveDocument ?? throw new InvalidOperationException("no drawing open");
        }

        static object Op(Document doc, Transaction tr, J r)
        {
            var db = doc.Database;
            string op = Str(r, "op");
            switch (op)
            {
                case "info":
                    {
                        var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                        var layers = new List<object>();
                        foreach (ObjectId id in lt)
                        {
                            var l = (LayerTableRecord)tr.GetObject(id, OpenMode.ForRead);
                            layers.Add(new { name = l.Name, color = l.Color.ColorIndex, off = l.IsOff, frozen = l.IsFrozen, locked = l.IsLocked });
                        }
                        db.UpdateExt(true); // EXTMIN/EXTMAX are only refreshed by a regen otherwise (deleted objects stay counted)
                        int n = 0;
                        foreach (ObjectId _ in Ms(tr, db, OpenMode.ForRead)) n++;
                        var blocks = new List<string>();
                        foreach (ObjectId id in (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead))
                        {
                            var b = (BlockTableRecord)tr.GetObject(id, OpenMode.ForRead);
                            if (!b.IsLayout && !b.IsAnonymous && !b.IsFromExternalReference) blocks.Add(b.Name);
                        }
                        return new
                        {
                            name = doc.Name, insunits = db.Insunits.ToString(), entities = n,
                            extmin = P(db.Extmin), extmax = P(db.Extmax), layers, blocks
                        };
                    }
                case "list":
                    {
                        string layer = Str(r, "layer", null), type = Str(r, "type", null);
                        int limit = Int(r, "limit", 500);
                        var outl = new List<object>();
                        foreach (ObjectId id in Ms(tr, db, OpenMode.ForRead))
                        {
                            var e = (Entity)tr.GetObject(id, OpenMode.ForRead);
                            if (layer != null && !string.Equals(e.Layer, layer, StringComparison.OrdinalIgnoreCase)) continue;
                            if (type != null && !string.Equals(id.ObjectClass.DxfName, type, StringComparison.OrdinalIgnoreCase)) continue;
                            outl.Add(Describe(e));
                            if (outl.Count >= limit) break;
                        }
                        return outl;
                    }
                case "get": return Describe((Entity)tr.GetObject(H(db, Str(r, "handle")), OpenMode.ForRead));
                case "layer":
                    {
                        string name = Str(r, "name");
                        var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                        LayerTableRecord l;
                        if (lt.Has(name)) l = (LayerTableRecord)tr.GetObject(lt[name], OpenMode.ForWrite);
                        else
                        {
                            lt.UpgradeOpen();
                            l = new LayerTableRecord { Name = name };
                            lt.Add(l); tr.AddNewlyCreatedDBObject(l, true);
                        }
                        if (Has(r, "color")) l.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(Autodesk.AutoCAD.Colors.ColorMethod.ByAci, (short)Int(r, "color", 7));
                        if (Has(r, "lineweight")) l.LineWeight = (LineWeight)Int(r, "lineweight", -3); // hundredths of mm: 35 = 0.35
                        if (Has(r, "linetype")) l.LinetypeObjectId = Linetype(tr, db, Str(r, "linetype"));
                        return name;
                    }
                case "line":
                    return Add(tr, db, r, new Line(new Point3d(D(r, "x1"), D(r, "y1"), 0), new Point3d(D(r, "x2"), D(r, "y2"), 0)));
                case "pline":
                    {
                        var pl = new Polyline();
                        int i = 0;
                        foreach (List<object> p in Arr(r, "points"))
                        {
                            double bulge = p.Count > 2 ? Num(p[2]) : 0;
                            pl.AddVertexAt(i++, new Point2d(Num(p[0]), Num(p[1])), bulge, 0, 0);
                        }
                        pl.Closed = Bool(r, "closed", false);
                        return Add(tr, db, r, pl);
                    }
                case "circle":
                    return Add(tr, db, r, new Circle(new Point3d(D(r, "cx"), D(r, "cy"), 0), Vector3d.ZAxis, D(r, "r")));
                case "arc":
                    return Add(tr, db, r, new Arc(new Point3d(D(r, "cx"), D(r, "cy"), 0), D(r, "r"),
                        Deg(D(r, "start")), Deg(D(r, "end"))));
                case "text":
                    {
                        var t = new MText
                        {
                            Location = new Point3d(D(r, "x"), D(r, "y"), 0),
                            TextHeight = D(r, "height"),
                            Contents = Str(r, "text"),
                            Rotation = Deg(Dbl(r, "rotation", 0)),
                            Attachment = (AttachmentPoint)Int(r, "attach", (int)AttachmentPoint.MiddleCenter),
                        };
                        if (Has(r, "width")) t.Width = D(r, "width");
                        return Add(tr, db, r, t);
                    }
                case "dim":
                    {
                        // aligned dimension between (x1,y1)-(x2,y2), dimension line through (dx,dy)
                        var dm = new AlignedDimension(new Point3d(D(r, "x1"), D(r, "y1"), 0), new Point3d(D(r, "x2"), D(r, "y2"), 0),
                            new Point3d(D(r, "dx"), D(r, "dy"), 0), Str(r, "text", ""), db.Dimstyle);
                        string h = Add(tr, db, r, dm);
                        if (Has(r, "scale")) dm.Dimscale = D(r, "scale"); // overall dim scale (DIMSCALE)
                        return h;
                    }
                case "hatch":
                    {
                        // boundary: closed polygon points [[x,y],...]; pattern default SOLID
                        var bnd = new Polyline();
                        int i = 0;
                        foreach (List<object> p in Arr(r, "points"))
                            bnd.AddVertexAt(i++, new Point2d(Num(p[0]), Num(p[1])), 0, 0, 0);
                        bnd.Closed = true;
                        bnd.SetDatabaseDefaults(db);
                        if (!_target.IsNull) bnd.Layer = "0";
                        ApplyProps(tr, db, r, bnd); // a kept boundary sits on the hatch's layer
                        var ms = Target(tr, db);
                        ms.AppendEntity(bnd); tr.AddNewlyCreatedDBObject(bnd, true);
                        var h = new Hatch();
                        h.SetDatabaseDefaults(db);
                        if (!_target.IsNull) h.Layer = "0";
                        ApplyProps(tr, db, r, h);
                        ms.AppendEntity(h); tr.AddNewlyCreatedDBObject(h, true);
                        // scale/angle must be set BEFORE SetHatchPattern, or the pattern is built at scale 1
                        if (Has(r, "scale")) h.PatternScale = D(r, "scale");
                        if (Has(r, "angle")) h.PatternAngle = Deg(D(r, "angle"));
                        h.SetHatchPattern(HatchPatternType.PreDefined, Str(r, "pattern", "SOLID"));
                        h.Associative = false;
                        h.AppendLoop(HatchLoopTypes.Outermost, new ObjectIdCollection { bnd.ObjectId });
                        h.EvaluateHatch(true);
                        if (!Bool(r, "keep_boundary", false)) bnd.Erase();
                        return h.Handle.ToString();
                    }
                case "insert":
                    {
                        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                        string name = Str(r, "name");
                        if (!bt.Has(name)) throw new ArgumentException("block not defined: " + name);
                        double sc = Dbl(r, "scale", 1);
                        var br = new BlockReference(new Point3d(D(r, "x"), D(r, "y"), Dbl(r, "z", 0)), bt[name])
                        {
                            Rotation = Deg(Dbl(r, "rotation", 0)),
                            ScaleFactors = new Scale3d(Dbl(r, "xscale", sc), Dbl(r, "yscale", sc), Dbl(r, "zscale", sc)) // negative = mirrored
                        };
                        return Add(tr, db, r, br);
                    }
                case "block_define":
                    {
                        // {"name", "entities":[create ops in block coordinates, base point = 0,0]}; redefines in place
                        string name = Str(r, "name");
                        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                        BlockTableRecord btr;
                        if (bt.Has(name))
                        {
                            btr = (BlockTableRecord)tr.GetObject(bt[name], OpenMode.ForWrite);
                            foreach (ObjectId id in btr) tr.GetObject(id, OpenMode.ForWrite).Erase();
                        }
                        else
                        {
                            bt.UpgradeOpen();
                            btr = new BlockTableRecord { Name = name, Origin = Point3d.Origin };
                            bt.Add(btr); tr.AddNewlyCreatedDBObject(btr, true);
                        }
                        if (!_target.IsNull) throw new InvalidOperationException("nested block_define");
                        _target = btr.ObjectId;
                        try { foreach (J sub in Arr(r, "entities")) Op(doc, tr, sub); }
                        finally { _target = ObjectId.Null; }
                        return name;
                    }
                case "block_import":
                    {
                        // {"path": "x.dwg", "name"?}: the DWG's model space becomes a block definition
                        string path = Str(r, "path");
                        string name = Str(r, "name", Path.GetFileNameWithoutExtension(path));
                        var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                        if (!bt.Has(name) || Bool(r, "overwrite", false))
                        {
                            using var src = new Database(false, true);
                            src.ReadDwgFile(path, FileOpenMode.OpenForReadAndAllShare, true, "");
                            db.Insert(name, src, true);
                        }
                        return BlockSummary(tr, db, name);
                    }
                case "block_info": return BlockSummary(tr, db, Str(r, "name"));
                case "delete":
                    {
                        int n = 0;
                        foreach (string h in Arr(r, "handles"))
                        { ((Entity)tr.GetObject(H(db, h), OpenMode.ForWrite)).Erase(); n++; }
                        return n;
                    }
                case "move":
                case "copy":
                case "rotate":
                    {
                        Matrix3d m = op == "rotate"
                            ? Matrix3d.Rotation(Deg(D(r, "angle")), Vector3d.ZAxis, new Point3d(D(r, "bx"), D(r, "by"), 0))
                            : Matrix3d.Displacement(new Vector3d(D(r, "dx"), D(r, "dy"), Dbl(r, "dz", 0)));
                        var outh = new List<string>();
                        var ms = op == "copy" ? Ms(tr, db, OpenMode.ForWrite) : null;
                        foreach (string hh in Arr(r, "handles"))
                        {
                            var e = (Entity)tr.GetObject(H(db, hh), OpenMode.ForWrite);
                            if (op == "copy")
                            {
                                var c = (Entity)e.Clone(); c.TransformBy(m);
                                ms.AppendEntity(c); tr.AddNewlyCreatedDBObject(c, true);
                                outh.Add(c.Handle.ToString());
                            }
                            else { e.TransformBy(m); outh.Add(e.Handle.ToString()); }
                        }
                        return outh;
                    }
                case "set":
                    {
                        var e = (Entity)tr.GetObject(H(db, Str(r, "handle")), OpenMode.ForWrite);
                        ApplyProps(tr, db, r, e);
                        if (e is MText mt && Has(r, "text")) mt.Contents = Str(r, "text");
                        if (e is DBText dt && Has(r, "text")) dt.TextString = Str(r, "text");
                        return e.Handle.ToString();
                    }
                // ---- 3D, through the database: always WCS, never a command prompt ----
                case "box":
                    {
                        Point3d a = Pt(Need(r, "min")), b = Pt(Need(r, "max"));
                        var s = new Solid3d();
                        s.CreateBox(Math.Abs(b.X - a.X), Math.Abs(b.Y - a.Y), Math.Abs(b.Z - a.Z));
                        s.TransformBy(Matrix3d.Displacement(new Vector3d((a.X + b.X) / 2, (a.Y + b.Y) / 2, (a.Z + b.Z) / 2)));
                        if (Has(r, "rotation")) s.TransformBy(Matrix3d.Rotation(Deg(D(r, "rotation")), Vector3d.ZAxis, a)); // about the min corner
                        return Made(tr, db, r, s);
                    }
                case "cylinder":
                    {
                        // base centre, radius r, height h along axis (default +Z); "r2" = top radius (a cone frustum)
                        double h = D(r, "h"), rad = D(r, "r");
                        var axis = Has(r, "axis") ? Pt(r["axis"]) - Point3d.Origin : Vector3d.ZAxis;
                        var s = new Solid3d();
                        s.CreateFrustum(h, rad, rad, Dbl(r, "r2", rad));
                        s.TransformBy(Matrix3d.PlaneToWorld(new Plane(Pt(Need(r, "base")), axis)) * Matrix3d.Displacement(new Vector3d(0, 0, h / 2)));
                        return Made(tr, db, r, s);
                    }
                case "sphere":
                    {
                        var s = new Solid3d();
                        s.CreateSphere(D(r, "r"));
                        s.TransformBy(Matrix3d.Displacement(Pt(Need(r, "center")) - Point3d.Origin));
                        return Made(tr, db, r, s);
                    }
                case "extrude":
                    {
                        // a closed outline: "points" [[x,y],...] at elevation "z", or the "handle" of a closed curve; "height" may be negative
                        Curve profile;
                        if (Has(r, "handle")) profile = (Curve)tr.GetObject(H(db, Str(r, "handle")), OpenMode.ForRead);
                        else
                        {
                            var pl = new Polyline();
                            int i = 0;
                            foreach (List<object> p in Arr(r, "points")) pl.AddVertexAt(i++, new Point2d(Num(p[0]), Num(p[1])), p.Count > 2 ? Num(p[2]) : 0, 0, 0);
                            pl.Closed = true;
                            pl.Elevation = Dbl(r, "z", 0);
                            profile = pl;
                        }
                        var regions = Region.CreateFromCurves(new DBObjectCollection { profile });
                        if (regions.Count == 0) throw new ArgumentException("the outline is not closed");
                        var s = new Solid3d();
                        s.Extrude((Region)regions[0], D(r, "height"), 0);
                        foreach (DBObject o in regions) o.Dispose();
                        if (!Has(r, "handle")) profile.Dispose();
                        else if (Bool(r, "erase_profile", false)) { profile.UpgradeOpen(); profile.Erase(); }
                        return Made(tr, db, r, s);
                    }
                case "union":
                case "subtract":
                case "intersect":
                    {
                        // the tools are merged into / cut from / intersected with the target, then erased; returns the target
                        var kind = op == "union" ? BooleanOperationType.BoolUnite : op == "subtract" ? BooleanOperationType.BoolSubtract : BooleanOperationType.BoolIntersect;
                        string th = Str(r, "target");
                        var t = tr.GetObject(H(db, th), OpenMode.ForWrite) as Solid3d ?? throw new ArgumentException(th + " is not a 3D solid");
                        foreach (string hh in Arr(r, "tools"))
                        {
                            var o = tr.GetObject(H(db, hh), OpenMode.ForWrite) as Solid3d ?? throw new ArgumentException(hh + " is not a 3D solid");
                            t.BooleanOperation(kind, o);
                            if (!o.IsErased) o.Erase();
                        }
                        return Describe(t);
                    }
                case "face3d":
                    {
                        var p = Arr(r, "points").Select(Pt).ToList();
                        if (p.Count < 3 || p.Count > 4) throw new ArgumentException("face3d takes 3 or 4 points");
                        return Made(tr, db, r, new Face(p[0], p[1], p[2], p[p.Count - 1], true, true, true, true));
                    }
                case "poly3d":
                    return Made(tr, db, r, new Polyline3d(Poly3dType.SimplePoly, new Point3dCollection(Arr(r, "points").Select(Pt).ToArray()), Bool(r, "closed", false)));
                case "getvar": return AcApp.GetSystemVariable(Str(r, "name"))?.ToString();
                case "setvar":
                    {
                        var cur = AcApp.GetSystemVariable(Str(r, "name"));
                        object v = cur is short ? (object)(short)Int(r, "value", 0)
                                 : cur is int ? Int(r, "value", 0)
                                 : cur is double ? Dbl(r, "value", 0)
                                 : (object)Convert.ToString(r["value"], Inv);
                        try { AcApp.SetSystemVariable(Str(r, "name"), v); }
                        catch (Autodesk.AutoCAD.Runtime.Exception ex) when (ex.ErrorStatus == ErrorStatus.InvalidInput)
                        {
                            // CAD&PILLAR refuses SetSystemVariable from a plugin in every context (measured), LISP is accepted.
                            throw new InvalidOperationException("this CAD refuses setvar from the plugin: use cad_lisp " +
                                "'(setvar \"" + Str(r, "name") + "\" " + (cur is string ? "\"" + v + "\"" : Convert.ToString(v, Inv)) + ")'");
                        }
                        return true;
                    }
                default: throw new ArgumentException("unknown op: " + op);
            }
        }

        // ---------------- helpers ----------------
        static BlockTableRecord Ms(Transaction tr, Database db, OpenMode mode)
        {
            return (BlockTableRecord)tr.GetObject(SymbolUtilityServices.GetBlockModelSpaceId(db), mode);
        }

        // Where creates go: model space, or the block definition being built by "block_define".
        static ObjectId _target = ObjectId.Null;
        static BlockTableRecord Target(Transaction tr, Database db)
        {
            return _target.IsNull ? Ms(tr, db, OpenMode.ForWrite) : (BlockTableRecord)tr.GetObject(_target, OpenMode.ForWrite);
        }

        static string Add(Transaction tr, Database db, J r, Entity e)
        {
            e.SetDatabaseDefaults(db);
            if (!_target.IsNull) e.Layer = "0"; // block content on 0 follows the INSERT's layer
            ApplyProps(tr, db, r, e);
            var ms = Target(tr, db);
            ms.AppendEntity(e);
            tr.AddNewlyCreatedDBObject(e, true);
            return e.Handle.ToString();
        }

        // Creates that also return their extents, so the caller can check the geometry without another call.
        static object Made(Transaction tr, Database db, J r, Entity e)
        {
            string h = Add(tr, db, r, e);
            var d = new J { ["handle"] = h };
            try { var ex = e.GeometricExtents; d["min"] = P(ex.MinPoint); d["max"] = P(ex.MaxPoint); } catch { }
            return d;
        }

        static Point3d Pt(object v)
        {
            var p = (List<object>)v;
            return new Point3d(Num(p[0]), Num(p[1]), p.Count > 2 ? Num(p[2]) : 0);
        }

        static object BlockSummary(Transaction tr, Database db, string name)
        {
            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            if (!bt.Has(name)) throw new ArgumentException("block not defined: " + name);
            var btr = (BlockTableRecord)tr.GetObject(bt[name], OpenMode.ForRead);
            Extents3d? ext = null;
            int n = 0;
            var types = new Dictionary<string, int>();
            foreach (ObjectId id in btr)
            {
                n++;
                types[id.ObjectClass.DxfName] = types.TryGetValue(id.ObjectClass.DxfName, out int k) ? k + 1 : 1;
                try
                {
                    var e = (Entity)tr.GetObject(id, OpenMode.ForRead);
                    var ge = e.GeometricExtents;
                    if (ext == null) ext = ge; else { var x = ext.Value; x.AddExtents(ge); ext = x; }
                }
                catch { }
            }
            return new
            {
                name, entities = n, types, insunits = btr.Units.ToString(),
                min = ext == null ? null : P(ext.Value.MinPoint), max = ext == null ? null : P(ext.Value.MaxPoint)
            };
        }

        static void ApplyProps(Transaction tr, Database db, J r, Entity e)
        {
            if (Has(r, "layer"))
            {
                string name = Str(r, "layer");
                var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                if (!lt.Has(name)) // create missing layers instead of failing
                {
                    lt.UpgradeOpen();
                    var l = new LayerTableRecord { Name = name };
                    lt.Add(l); tr.AddNewlyCreatedDBObject(l, true);
                }
                e.Layer = name;
            }
            if (Has(r, "color"))
                e.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(Autodesk.AutoCAD.Colors.ColorMethod.ByAci, (short)Int(r, "color", 256));
        }

        static readonly Dictionary<string, string> LtItalian = new Dictionary<string, string>
        {
            ["BORDER"] = "BORDO", ["CENTER"] = "CENTRO", ["DASHDOT"] = "TRATTOPUNTO", ["DASHED"] = "TRATTEGGIATA",
            ["DIVIDE"] = "DIVIDI", ["DOT"] = "PUNTO", ["HIDDEN"] = "NASCOSTA", ["PHANTOM"] = "FANTASMA",
        };

        static ObjectId Linetype(Transaction tr, Database db, string name)
        {
            var lt = (LinetypeTable)tr.GetObject(db.LinetypeTableId, OpenMode.ForRead);
            if (lt.Has(name)) return lt[name];
            // Each product ships its own files: AutoCAD acad(iso).lin, CAD&PILLAR cadp(iso).lin.
            var files = db.Measurement == MeasurementValue.Metric
                ? new[] { "acadiso.lin", "cadpiso.lin", "acad.lin", "cadp.lin" }
                : new[] { "acad.lin", "cadp.lin", "acadiso.lin", "cadpiso.lin" };
            // The Italian CAD&PILLAR files name the standard linetypes in Italian: DASHED is TRATTEGGIATA there.
            var m = System.Text.RegularExpressions.Regex.Match(name.ToUpperInvariant(), "^(BORDER|CENTER|DASHDOT|DASHED|DIVIDE|DOT|HIDDEN|PHANTOM)(2|X2)?$");
            var names = new List<string> { name };
            if (m.Success) names.Add(LtItalian[m.Groups[1].Value] + m.Groups[2].Value);
            foreach (var n in names) if (lt.Has(n)) return lt[n];
            var found = new List<string>();
            foreach (var f in files)
            {
                try { found.Add(HostApplicationServices.Current.FindFile(f, db, FindFileHint.Default)); } catch { }
            }
            foreach (var n in names)
                foreach (var path in found)
                {
                    try { db.LoadLineTypeFile(n, path); } catch { continue; }
                    if (lt.Has(n)) return lt[n];
                }
            throw new ArgumentException("linetype '" + string.Join("' / '", names) + "' is in none of: " +
                (found.Count > 0 ? string.Join(", ", found.Select(f => Path.GetFileName(f))) : string.Join(", ", files) + " (none found)"));
        }

        static object Describe(Entity e)
        {
            var d = new Dictionary<string, object>
            {
                ["handle"] = e.Handle.ToString(), ["type"] = e.GetRXClass().DxfName, ["layer"] = e.Layer, ["color"] = e.Color.ColorIndex
            };
            try { var ex = e.GeometricExtents; d["min"] = P(ex.MinPoint); d["max"] = P(ex.MaxPoint); } catch { }
            switch (e)
            {
                case Line l: d["start"] = P(l.StartPoint); d["end"] = P(l.EndPoint); break;
                case Polyline pl:
                    d["closed"] = pl.Closed;
                    d["points"] = Enumerable.Range(0, pl.NumberOfVertices).Select(i => new[] { pl.GetPoint2dAt(i).X, pl.GetPoint2dAt(i).Y, pl.GetBulgeAt(i) }).ToList();
                    break;
                case Circle c: d["center"] = P(c.Center); d["r"] = c.Radius; break;
                case Arc a: d["center"] = P(a.Center); d["r"] = a.Radius; d["start"] = a.StartAngle * 180 / Math.PI; d["end"] = a.EndAngle * 180 / Math.PI; break;
                case MText m: d["text"] = m.Contents; d["at"] = P(m.Location); d["height"] = m.TextHeight; break;
                case DBText t: d["text"] = t.TextString; d["at"] = P(t.Position); d["height"] = t.Height; break;
                case BlockReference b:
                    d["name"] = b.Name; d["at"] = P(b.Position); d["rotation"] = b.Rotation * 180 / Math.PI;
                    d["scale"] = new[] { b.ScaleFactors.X, b.ScaleFactors.Y, b.ScaleFactors.Z };
                    break;
                case Solid3d so:
                    try { d["volume"] = so.MassProperties.Volume; } catch { }
                    break;
            }
            return d;
        }

        static ObjectId H(Database db, string handle)
        {
            if (!db.TryGetObjectId(new Handle(Convert.ToInt64(handle, 16)), out ObjectId id) || id.IsErased)
                throw new ArgumentException("unknown handle: " + handle);
            return id;
        }

        static double[] P(Point3d p) => new[] { Math.Round(p.X, 6), Math.Round(p.Y, 6), Math.Round(p.Z, 6) };
        static double Deg(double a) => a * Math.PI / 180.0;
        // ---- request access (requests are MiniJson dictionaries; JSON null counts as missing) ----
        static readonly System.Globalization.CultureInfo Inv = System.Globalization.CultureInfo.InvariantCulture;
        static bool Has(J r, string k) => r.TryGetValue(k, out var v) && v != null;
        static object Need(J r, string k) => Has(r, k) ? r[k] : throw new ArgumentException("missing '" + k + "'");
        static double Num(object v) => Convert.ToDouble(v, Inv);
        static IEnumerable<object> Arr(J r, string k) => (List<object>)Need(r, k);
        static string Str(J r, string k) => Convert.ToString(Need(r, k), Inv);
        static string Str(J r, string k, string dflt) => Has(r, k) ? Convert.ToString(r[k], Inv) : dflt;
        static double D(J r, string k) => Num(Need(r, k));
        static double Dbl(J r, string k, double dflt) => Has(r, k) ? Num(r[k]) : dflt;
        static int Int(J r, string k, int dflt) => Has(r, k) ? Convert.ToInt32(Num(r[k])) : dflt;
        static bool Bool(J r, string k, bool dflt) => Has(r, k) ? (bool)r[k] : dflt;
    }

    // Minimal JSON for both runtimes (.NET Framework 4.8 has no System.Text.Json).
    // Parse: objects -> Dictionary<string, object>, arrays -> List<object>, numbers -> double.
    // Write: null, string, bool, numbers, IDictionary, IEnumerable, else public properties (anonymous results).
    public static class MiniJson
    {
        static readonly System.Globalization.CultureInfo Inv = System.Globalization.CultureInfo.InvariantCulture;

        public static object Parse(string s)
        {
            int i = 0;
            var v = Value(s, ref i);
            Ws(s, ref i);
            if (i != s.Length) throw new FormatException("JSON: trailing data at " + i);
            return v;
        }

        static void Ws(string s, ref int i) { while (i < s.Length && char.IsWhiteSpace(s[i])) i++; }

        static object Value(string s, ref int i)
        {
            Ws(s, ref i);
            if (i >= s.Length) throw new FormatException("JSON: unexpected end");
            char c = s[i];
            if (c == '{')
            {
                var d = new Dictionary<string, object>(); i++; Ws(s, ref i);
                if (s[i] == '}') { i++; return d; }
                while (true)
                {
                    Ws(s, ref i); string k = Str(s, ref i); Ws(s, ref i);
                    if (s[i++] != ':') throw new FormatException("JSON: ':' expected at " + (i - 1));
                    d[k] = Value(s, ref i); Ws(s, ref i);
                    if (s[i] == ',') { i++; continue; }
                    if (s[i++] == '}') return d;
                    throw new FormatException("JSON: ',' or '}' expected at " + (i - 1));
                }
            }
            if (c == '[')
            {
                var l = new List<object>(); i++; Ws(s, ref i);
                if (s[i] == ']') { i++; return l; }
                while (true)
                {
                    l.Add(Value(s, ref i)); Ws(s, ref i);
                    if (s[i] == ',') { i++; continue; }
                    if (s[i++] == ']') return l;
                    throw new FormatException("JSON: ',' or ']' expected at " + (i - 1));
                }
            }
            if (c == '"') return Str(s, ref i);
            if (string.CompareOrdinal(s, i, "true", 0, 4) == 0) { i += 4; return true; }
            if (string.CompareOrdinal(s, i, "false", 0, 5) == 0) { i += 5; return false; }
            if (string.CompareOrdinal(s, i, "null", 0, 4) == 0) { i += 4; return null; }
            int st = i;
            while (i < s.Length && "+-0123456789.eE".IndexOf(s[i]) >= 0) i++;
            if (st == i) throw new FormatException("JSON: unexpected '" + c + "' at " + i);
            return double.Parse(s.Substring(st, i - st), System.Globalization.NumberStyles.Float, Inv);
        }

        static string Str(string s, ref int i)
        {
            if (s[i] != '"') throw new FormatException("JSON: string expected at " + i);
            var b = new StringBuilder(); i++;
            while (true)
            {
                char c = s[i++];
                if (c == '"') return b.ToString();
                if (c != '\\') { b.Append(c); continue; }
                char e = s[i++];
                switch (e)
                {
                    case 'n': b.Append('\n'); break;
                    case 't': b.Append('\t'); break;
                    case 'r': b.Append('\r'); break;
                    case 'b': b.Append('\b'); break;
                    case 'f': b.Append('\f'); break;
                    case 'u': b.Append((char)Convert.ToInt32(s.Substring(i, 4), 16)); i += 4; break;
                    default: b.Append(e); break; // quote, backslash, slash
                }
            }
        }

        public static string Write(object v) { var b = new StringBuilder(); W(b, v); return b.ToString(); }

        static void W(StringBuilder b, object v)
        {
            switch (v)
            {
                case null: b.Append("null"); return;
                case string s: Q(b, s); return;
                case bool x: b.Append(x ? "true" : "false"); return;
                case double d: b.Append(double.IsNaN(d) || double.IsInfinity(d) ? "null" : d.ToString("R", Inv)); return;
                case float f: W(b, (double)f); return;
                case int _: case long _: case short _: case uint _: case ulong _: case ushort _: case byte _: case decimal _:
                    b.Append(Convert.ToString(v, Inv)); return;
                case Enum en: Q(b, en.ToString()); return;
                case System.Collections.IDictionary dict:
                    {
                        b.Append('{'); bool first = true;
                        foreach (System.Collections.DictionaryEntry kv in dict)
                        { if (!first) b.Append(','); first = false; Q(b, Convert.ToString(kv.Key, Inv)); b.Append(':'); W(b, kv.Value); }
                        b.Append('}'); return;
                    }
                case System.Collections.IEnumerable seq:
                    {
                        b.Append('['); bool first = true;
                        foreach (var x in seq) { if (!first) b.Append(','); first = false; W(b, x); }
                        b.Append(']'); return;
                    }
                default:
                    {
                        b.Append('{'); bool first = true;
                        foreach (var p in v.GetType().GetProperties())
                        { if (!first) b.Append(','); first = false; Q(b, p.Name); b.Append(':'); W(b, p.GetValue(v, null)); }
                        b.Append('}'); return;
                    }
            }
        }

        static void Q(StringBuilder b, string s)
        {
            b.Append('"');
            foreach (char c in s)
            {
                if (c == '"' || c == '\\') b.Append('\\').Append(c);
                else if (c < ' ') b.Append("\\u").Append(((int)c).ToString("x4"));
                else b.Append(c);
            }
            b.Append('"');
        }
    }
}
