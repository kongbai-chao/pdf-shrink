param(
  [Parameter(Mandatory=$true)][string]$Pdf,
  [Parameter(Mandatory=$true)][string]$Out,
  [int]$Width = 2480,
  [int]$Page = 0
)
# Renders one page of a PDF to PNG (or JPEG) using the Windows.Data.Pdf engine.
# Width is in DIPs; the output pixel width is Width * (display scale), typically
# Width * 1.5 on a 150% scaled desktop, so pass dpi*8.2677/1.5 for a target DPI.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
})[0]

function AwaitOp($op, $type) {
  $m = $asTaskGeneric.MakeGenericMethod($type)
  $t = $m.Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  return $t.Result
}
function AwaitAction($op) {
  $t = $asTaskAction.Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
}

[Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null

$bytes = [System.IO.File]::ReadAllBytes($Pdf)
$mem = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$writer = New-Object Windows.Storage.Streams.DataWriter($mem)
$writer.WriteBytes($bytes)
AwaitOp ($writer.StoreAsync()) ([uint32]) | Out-Null
$writer.DetachStream() | Out-Null

$doc = AwaitOp ([Windows.Data.Pdf.PdfDocument]::LoadFromStreamAsync($mem)) ([Windows.Data.Pdf.PdfDocument])
$pg = $doc.GetPage([uint32]$Page)

$opts = New-Object Windows.Data.Pdf.PdfPageRenderOptions
$opts.DestinationWidth = [uint32]$Width
$opts.IsIgnoringHighContrast = $true

$outStream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
AwaitAction ($pg.RenderToStreamAsync($outStream, $opts))

$n = [int]$outStream.Size
$inS = $outStream.GetInputStreamAt(0)
$rd = New-Object Windows.Storage.Streams.DataReader($inS)
AwaitOp ($rd.LoadAsync([uint32]$n)) ([uint32]) | Out-Null
$buf = New-Object byte[] $n
$rd.ReadBytes($buf)
$rd.Dispose()
$pg.Dispose()
[System.IO.File]::WriteAllBytes($Out, $buf)
Write-Output ("WROTE " + $Out + " (" + $buf.Length + ")")
