param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [string]$OutputDir = "output",
    [string]$Model = "PaddleOCR-VL-1.6",
    [int]$PollIntervalSeconds = 5,
    [int]$MaxWaitSeconds = 10800,
    [switch]$DocOrientation,
    [switch]$DocUnwarping,
    [switch]$ChartRecognition
)

$ErrorActionPreference = "Stop"
$JobUrl = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"

function Get-DotEnvValue {
    param([string]$Name)

    $paths = @(
        (Join-Path (Get-Location) ".env"),
        (Join-Path $PSScriptRoot ".env")
    ) | Select-Object -Unique

    foreach ($path in $paths) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            continue
        }

        foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
            $trimmed = $line.Trim()
            if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
                continue
            }
            $key, $value = $trimmed.Split("=", 2)
            if ($key.Trim() -eq $Name) {
                return $value.Trim().Trim('"').Trim("'")
            }
        }
    }

    return $null
}

$Token = if ($env:PADDLEOCR_TOKEN) { $env:PADDLEOCR_TOKEN } else { Get-DotEnvValue -Name "PADDLEOCR_TOKEN" }

if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Error 'Missing PaddleOCR token. Set it first in .env: PADDLEOCR_TOKEN=your-token'
}

function ConvertTo-CompactJson {
    param([object]$Value)
    return ($Value | ConvertTo-Json -Depth 20 -Compress)
}

function Get-SafeFileName {
    param([string]$Name)
    $invalid = [System.IO.Path]::GetInvalidFileNameChars()
    $safe = $Name
    foreach ($char in $invalid) {
        $safe = $safe.Replace($char, "_")
    }
    $safe = $safe.Trim(" .")
    if ([string]::IsNullOrWhiteSpace($safe)) {
        return "document"
    }
    return $safe
}

function Get-InputContentType {
    param([string]$Path)

    switch ([System.IO.Path]::GetExtension($Path).ToLowerInvariant()) {
        ".pdf" { return "application/pdf" }
        ".jpg" { return "image/jpeg" }
        ".jpeg" { return "image/jpeg" }
        ".png" { return "image/png" }
        ".tif" { return "image/tiff" }
        ".tiff" { return "image/tiff" }
        default {
            throw "Unsupported input format. Supported formats: .pdf, .jpg, .jpeg, .png, .tif, .tiff"
        }
    }
}

function Get-SafeOutputPath {
    param(
        [string]$Root,
        [string]$RelativePath
    )

    $relative = $RelativePath.Replace("/", [System.IO.Path]::DirectorySeparatorChar)
    if ([System.IO.Path]::IsPathRooted($relative) -or $relative.Split([System.IO.Path]::DirectorySeparatorChar) -contains "..") {
        $relative = [System.IO.Path]::GetFileName($relative)
    }

    $rootFull = [System.IO.Path]::GetFullPath($Root)
    $targetFull = [System.IO.Path]::GetFullPath((Join-Path $rootFull $relative))
    $separator = [System.IO.Path]::DirectorySeparatorChar
    $rootPrefix = $rootFull.TrimEnd($separator) + $separator
    if (
        -not $targetFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -and
        -not $targetFull.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "Unsafe output path from API: $RelativePath"
    }
    return $targetFull
}

function Submit-PaddleJob {
    param(
        [string]$PathOrUrl,
        [hashtable]$OptionalPayload
    )

    $headers = @{
        Authorization = "bearer $Token"
    }

    if ($PathOrUrl.StartsWith("http://") -or $PathOrUrl.StartsWith("https://")) {
        $payload = @{
            fileUrl = $PathOrUrl
            model = $Model
            optionalPayload = $OptionalPayload
        }
        $response = Invoke-RestMethod -Method Post -Uri $JobUrl -Headers $headers -ContentType "application/json" -Body (ConvertTo-CompactJson $payload)
        return $response.data.jobId
    }

    if (-not (Test-Path -LiteralPath $PathOrUrl -PathType Leaf)) {
        throw "File not found: $PathOrUrl"
    }

    Add-Type -AssemblyName System.Net.Http
    $client = [System.Net.Http.HttpClient]::new()
    $stream = $null
    $form = $null
    try {
        $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new("bearer", $Token)
        $form = [System.Net.Http.MultipartFormDataContent]::new()
        $form.Add([System.Net.Http.StringContent]::new($Model), "model")
        $form.Add([System.Net.Http.StringContent]::new((ConvertTo-CompactJson $OptionalPayload)), "optionalPayload")

        $stream = [System.IO.File]::OpenRead($PathOrUrl)
        $fileContent = [System.Net.Http.StreamContent]::new($stream)
        $fileContent.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse((Get-InputContentType -Path $PathOrUrl))
        $form.Add($fileContent, "file", [System.IO.Path]::GetFileName($PathOrUrl))

        $httpResponse = $client.PostAsync($JobUrl, $form).Result
        $body = $httpResponse.Content.ReadAsStringAsync().Result
        if (-not $httpResponse.IsSuccessStatusCode) {
            throw "Job submit failed, HTTP $([int]$httpResponse.StatusCode): $body"
        }
        $json = $body | ConvertFrom-Json
        return $json.data.jobId
    }
    finally {
        if ($form -ne $null) { $form.Dispose() }
        if ($stream -ne $null) { $stream.Dispose() }
        if ($client -ne $null) { $client.Dispose() }
    }
}

function Wait-PaddleResultUrl {
    param([string]$JobId)

    $headers = @{
        Authorization = "bearer $Token"
    }
    $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)

    while ($true) {
        if ((Get-Date) -gt $deadline) {
            throw "Timed out waiting for job $JobId after $MaxWaitSeconds seconds"
        }

        $response = Invoke-RestMethod -Method Get -Uri "$JobUrl/$JobId" -Headers $headers
        $state = $response.data.state

        if ($state -eq "pending") {
            Write-Host "Job status: pending"
        }
        elseif ($state -eq "running") {
            $progress = $response.data.extractProgress
            if ($progress -and $null -ne $progress.totalPages) {
                Write-Host "Job status: running, pages $($progress.extractedPages)/$($progress.totalPages)"
            }
            else {
                Write-Host "Job status: running"
            }
        }
        elseif ($state -eq "done") {
            Write-Host "Job completed"
            return $response.data.resultUrl.jsonUrl
        }
        elseif ($state -eq "failed") {
            throw "Job failed: $($response.data.errorMsg)"
        }
        else {
            Write-Host "Job status: $state"
        }

        Start-Sleep -Seconds $PollIntervalSeconds
    }
}

function Save-PaddleResults {
    param(
        [string]$JsonlUrl,
        [string]$RootDir,
        [string]$CombinedMd
    )

    New-Item -ItemType Directory -Force -Path $RootDir | Out-Null
    $pagesDir = Join-Path $RootDir "pages"
    New-Item -ItemType Directory -Force -Path $pagesDir | Out-Null
    New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($CombinedMd))) | Out-Null

    $jsonl = Invoke-WebRequest -Uri $JsonlUrl -UseBasicParsing
    $pageNum = 1
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $combinedWriter = [System.IO.StreamWriter]::new($CombinedMd, $false, $utf8NoBom)

    try {
        foreach ($line in ($jsonl.Content -split "`n")) {
            $trimmed = $line.Trim()
            if ([string]::IsNullOrWhiteSpace($trimmed)) {
                continue
            }

            $item = $trimmed | ConvertFrom-Json
            foreach ($layoutResult in $item.result.layoutParsingResults) {
                $markdown = $layoutResult.markdown
                $mdText = [string]$markdown.text

                $pageFile = Join-Path $pagesDir ("page_{0:d4}.md" -f $pageNum)
                [System.IO.File]::WriteAllText($pageFile, $mdText, $utf8NoBom)

                $combinedWriter.WriteLine("")
                $combinedWriter.WriteLine("")
                $combinedWriter.WriteLine("<!-- page $pageNum -->")
                $combinedWriter.WriteLine("")
                $combinedWriter.WriteLine($mdText.TrimEnd())

                if ($markdown.images) {
                    foreach ($property in $markdown.images.PSObject.Properties) {
                        $target = Get-SafeOutputPath -Root $RootDir -RelativePath $property.Name
                        New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($target)) | Out-Null
                        Invoke-WebRequest -Uri $property.Value -OutFile $target -UseBasicParsing
                    }
                }

                if ($layoutResult.outputImages) {
                    $outputImagesDir = Join-Path $RootDir "output_images"
                    New-Item -ItemType Directory -Force -Path $outputImagesDir | Out-Null
                    foreach ($property in $layoutResult.outputImages.PSObject.Properties) {
                        $safeName = Get-SafeFileName ("{0}_{1:d4}.jpg" -f $property.Name, $pageNum)
                        Invoke-WebRequest -Uri $property.Value -OutFile (Join-Path $outputImagesDir $safeName) -UseBasicParsing
                    }
                }

                Write-Host "Saved page $pageNum`: $pageFile"
                $pageNum += 1
            }
        }
    }
    finally {
        $combinedWriter.Dispose()
    }

    if ($pageNum -eq 1) {
        throw "No layoutParsingResults found in OCR result"
    }
}

$optionalPayload = @{
    useDocOrientationClassify = [bool]$DocOrientation
    useDocUnwarping = [bool]$DocUnwarping
    useChartRecognition = [bool]$ChartRecognition
}

$resolvedOutputDir = [System.IO.Path]::GetFullPath($OutputDir)
$inputStem = if ($InputPath.StartsWith("http://") -or $InputPath.StartsWith("https://")) { "document" } else { [System.IO.Path]::GetFileNameWithoutExtension($InputPath) }
$combinedMd = Join-Path $resolvedOutputDir ("{0}.md" -f (Get-SafeFileName $inputStem))

Write-Host "Submitting job: $InputPath"
$jobId = Submit-PaddleJob -PathOrUrl $InputPath -OptionalPayload $optionalPayload
Write-Host "Job submitted: $jobId"

$jsonlUrl = Wait-PaddleResultUrl -JobId $jobId
Save-PaddleResults -JsonlUrl $jsonlUrl -RootDir $resolvedOutputDir -CombinedMd $combinedMd

Write-Host "Combined Markdown saved: $combinedMd"
Write-Host "Output directory: $resolvedOutputDir"
