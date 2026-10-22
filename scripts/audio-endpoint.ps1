# Read the Windows default playback endpoint state (name, mute, volume).
# Read-only: uses Core Audio COM, no changes.
param([switch]$Unmute, [int]$SetVolume = -1, [double]$PeakSeconds = 0)

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDeviceEnumeratorComObject { }

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
    int NotImpl1();
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppDevice);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
    int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
    int OpenPropertyStore(int stgmAccess, out IPropertyStore ppProperties);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string ppstrId);
    int GetState(out int pdwState);
}

[ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore {
    int GetCount(out int cProps);
    int GetAt(int iProp, out PROPERTYKEY pkey);
    int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
}

[StructLayout(LayoutKind.Sequential)]
struct PROPERTYKEY { public Guid fmtid; public int pid; }

[StructLayout(LayoutKind.Explicit)]
struct PROPVARIANT {
    [FieldOffset(0)] public short vt;
    [FieldOffset(8)] public IntPtr pointerValue;
}

[ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
    int RegisterControlChangeNotify(IntPtr pNotify);
    int UnregisterControlChangeNotify(IntPtr pNotify);
    int GetChannelCount(out uint pnChannelCount);
    int SetMasterVolumeLevel(float fLevelDB, Guid pguidEventContext);
    int SetMasterVolumeLevelScalar(float fLevel, Guid pguidEventContext);
    int GetMasterVolumeLevel(out float pfLevelDB);
    int GetMasterVolumeLevelScalar(out float pfLevel);
    int SetChannelVolumeLevel(uint nChannel, float fLevelDB, Guid pguidEventContext);
    int SetChannelVolumeLevelScalar(uint nChannel, float fLevel, Guid pguidEventContext);
    int GetChannelVolumeLevel(uint nChannel, out float pfLevelDB);
    int GetChannelVolumeLevelScalar(uint nChannel, out float pfLevel);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, Guid pguidEventContext);
    int GetMute([MarshalAs(UnmanagedType.Bool)] out bool pbMute);
}

[ComImport, Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioMeterInformation {
    int GetPeakValue(out float pfPeak);
    int GetMeteringChannelCount(out int pnChannelCount);
    int GetChannelsPeakValues(int u32ChannelCount, [Out, MarshalAs(UnmanagedType.LPArray)] float[] afPeakValues);
    int QueryHardwareSupport(out int pdwHardwareSupportMask);
}

public static class Endpoint {
    static IMMDevice GetDefault() {
        var en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
        IMMDevice dev;
        Marshal.ThrowExceptionForHR(en.GetDefaultAudioEndpoint(0, 0, out dev));
        return dev;
    }
    public static string Name() {
        var dev = GetDefault();
        IPropertyStore ps; string id;
        dev.GetId(out id);
        dev.OpenPropertyStore(0, out ps);
        var key = new PROPERTYKEY();
        key.fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0");
        key.pid = 14;
        PROPVARIANT pv; ps.GetValue(ref key, out pv);
        try { return Marshal.PtrToStringUni(pv.pointerValue); } catch { return id; }
    }
    public static float Volume() {
        var dev = GetDefault();
        var iid = typeof(IAudioEndpointVolume).GUID;
        object o;
        dev.Activate(ref iid, 1, IntPtr.Zero, out o);
        float v; ((IAudioEndpointVolume)o).GetMasterVolumeLevelScalar(out v);
        return v;
    }
    public static bool Muted() {
        var dev = GetDefault();
        var iid = typeof(IAudioEndpointVolume).GUID;
        object o;
        dev.Activate(ref iid, 1, IntPtr.Zero, out o);
        bool m; ((IAudioEndpointVolume)o).GetMute(out m);
        return m;
    }
    public static void SetVol(float v) {
        var dev = GetDefault();
        var iid = typeof(IAudioEndpointVolume).GUID;
        object o;
        dev.Activate(ref iid, 1, IntPtr.Zero, out o);
        ((IAudioEndpointVolume)o).SetMasterVolumeLevelScalar(v, Guid.Empty);
    }
    public static void Unmute() {
        var dev = GetDefault();
        var iid = typeof(IAudioEndpointVolume).GUID;
        object o;
        dev.Activate(ref iid, 1, IntPtr.Zero, out o);
        ((IAudioEndpointVolume)o).SetMute(false, Guid.Empty);
    }
    public static float Peak() {
        var dev = GetDefault();
        var iid = typeof(IAudioMeterInformation).GUID;
        object o;
        dev.Activate(ref iid, 1, IntPtr.Zero, out o);
        float p;
        ((IAudioMeterInformation)o).GetPeakValue(out p);
        return p;
    }
}
'@

$name = [Endpoint]::Name()
$vol = [math]::Round([Endpoint]::Volume() * 100)
$muted = [Endpoint]::Muted()
"endpoint : $name"
"volume   : $vol%"
"muted    : $muted"

if ($SetVolume -ge 0) { [Endpoint]::SetVol($SetVolume / 100.0); "set volume -> $SetVolume%" }
if ($Unmute) { [Endpoint]::Unmute(); "set mute -> false" }
if ($PeakSeconds -gt 0) {
    $max = 0.0
    $n = [int]($PeakSeconds * 20)
    for ($i = 0; $i -lt $n; $i++) {
        $p = [Endpoint]::Peak()
        if ($p -gt $max) { $max = $p }
        Start-Sleep -Milliseconds 50
    }
    "peak max : {0:P1}" -f $max
}
