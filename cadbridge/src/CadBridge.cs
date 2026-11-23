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
            var tcs = new TaskCompletionSource<string>();
            RunOnMain(() =>
            {
                try
                {
                    var res = Dispatch((J)MiniJson.Parse(line));
                    tcs.SetResult(MiniJson.Write(new Dictionary<string, object> { ["ok"] = true, ["result"] = res }));
                }
                catch (System.Exception ex)
                {
                    tcs.SetResult(MiniJson.Write(new Dictionary<string, object> { ["ok"] = false, ["error"] = ex.GetType().Name + ": " + ex.Message }));
                }
            });
            // ponytail: fixed 120 s wait; a modal dialog in CAD blocks the main thread, so the caller gets a timeout.
            if (!tcs.Task.Wait(TimeSpan.FromSeconds(120)))
                return "{\"ok\":false,\"error\":\"timeout: CAD main thread busy (modal dialog or running command?)\"}";
            return tcs.Task.Result;
        }

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
                        // Asynchronous: queued on the command line of the active drawing.
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
                        var br = new BlockReference(new Point3d(D(r, "x"), D(r, "y"), 0), bt[name])
                        {
                            Rotation = Deg(Dbl(r, "rotation", 0)), ScaleFactors = new Scale3d(Dbl(r, "scale", 1))
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
                            : Matrix3d.Displacement(new Vector3d(D(r, "dx"), D(r, "dy"), 0));
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
                case "getvar": return AcApp.GetSystemVariable(Str(r, "name"))?.ToString();
                case "setvar":
                    {
                        var cur = AcApp.GetSystemVariable(Str(r, "name"));
                        object v = cur is short ? (object)(short)Int(r, "value", 0)
                                 : cur is int ? Int(r, "value", 0)
                                 : cur is double ? Dbl(r, "value", 0)
                                 : (object)Convert.ToString(r["value"], Inv);
                        AcApp.SetSystemVariable(Str(r, "name"), v);
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

        static ObjectId Linetype(Transaction tr, Database db, string name)
        {
            var lt = (LinetypeTable)tr.GetObject(db.LinetypeTableId, OpenMode.ForRead);
            if (!lt.Has(name)) db.LoadLineTypeFile(name, "acadiso.lin");
            return lt[name];
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
                case BlockReference b: d["name"] = b.Name; d["at"] = P(b.Position); d["rotation"] = b.Rotation * 180 / Math.PI; break;
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
