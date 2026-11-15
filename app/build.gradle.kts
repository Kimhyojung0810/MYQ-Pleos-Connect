import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
}

// Dev-only values come from local.properties (git-ignored). Never commit real keys.
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
fun localOr(key: String, default: String) = localProps.getProperty(key) ?: default
/** Server for the release build; when absent, release = self-contained 체험 APK. */
val prodUrl: String? = localProps.getProperty("roadpod.prodUrl")

android {
    namespace = "ai.roadpod.app"
    compileSdk = 34

    // AAOS Car API (CarPropertyManager for PERF_VEHICLE_SPEED etc.)
    useLibrary("android.car")

    defaultConfig {
        applicationId = "ai.roadpod.app"
        minSdk = 28
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"

        // Emulator reaches the host PC at 10.0.2.2
        buildConfigField("String", "ROADPOD_BASE_URL", "\"${localOr("roadpod.baseUrl", "http://10.0.2.2:8765")}\"")
        buildConfigField("String", "ROADPOD_DEV_TOKEN", "\"${localOr("roadpod.devToken", "local-demo-token")}\"")
        buildConfigField("boolean", "MOCK", "false")
    }

    // Two builds from one codebase:
    //   debug   = 데모용: adb scenario simulator (src/debug), dev log on screen, local server (10.0.2.2), label "MY Q 데모"
    //   release = 제출·서비스용: no debug hooks. No roadpod.prodUrl → self-contained 체험 APK; with it → that server
    //   mock    = 체험용 (제출 APK): show runs in the app on a script, label "MY Q 체험"
    // Same applicationId on purpose: Pleos permissions and the emulator setup stay as they are.
    signingConfigs {
        create("release") {
            val ks = localProps.getProperty("release.storeFile")
            if (ks != null) {
                storeFile = rootProject.file(ks)
                storePassword = localProps.getProperty("release.storePassword")
                keyAlias = localProps.getProperty("release.keyAlias")
                keyPassword = localProps.getProperty("release.keyPassword")
            }
        }
    }
    buildTypes {
        getByName("debug") {
            versionNameSuffix = "-demo"
            manifestPlaceholders["appLabel"] = "MY Q 데모"
        }
        getByName("release") {
            isMinifyEnabled = false
            manifestPlaceholders["appLabel"] = "MY Q"
            // Without roadpod.prodUrl the release APK is self-contained (체험 mode: in-app show, no server, no API key).
            // With it, the release APK talks to that server (GPT-generated show).
            buildConfigField("String", "ROADPOD_BASE_URL", "\"${prodUrl ?: "http://local.myq"}\"")
            buildConfigField("String", "ROADPOD_DEV_TOKEN", "\"${localOr("roadpod.prodToken", "")}\"")
            buildConfigField("boolean", "MOCK", if (prodUrl == null) "true" else "false")
            // Until a release keystore exists (release.* in local.properties), sign with the debug key so it still installs.
            signingConfig = if (localProps.getProperty("release.storeFile") != null) signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
        // 체험(목업) build for submission: no server, no API key. The show is an in-app script with bundled
        // voices (src/mock/assets); Pleos vehicle data, navigation, voice input and the screen are the real ones.
        create("mock") {
            initWith(getByName("debug"))
            isDebuggable = false          // no developer log on the submitted APK
            versionNameSuffix = "-mock"
            manifestPlaceholders["appLabel"] = "MY Q 체험"
            buildConfigField("boolean", "MOCK", "true")
            matchingFallbacks += listOf("debug")
        }
    }
    // The 체험 build keeps the adb scenario hooks of the demo build (handy on the emulator).
    sourceSets.maybeCreate("mock").apply {
        java.srcDir("src/debug/java")
        manifest.srcFile("src/debug/AndroidManifest.xml")
    }
    // The self-contained release (submission APK) ships the bundled show (voices, diary image) and the adb
    // scenario hooks, so the features can be tried on the emulator. The server-connected release has neither.
    if (prodUrl == null) sourceSets.maybeCreate("release").apply {
        assets.srcDir("src/mock/assets")
        java.srcDir("src/debug/java")
        manifest.srcFile("src/debug/AndroidManifest.xml")
    }

    buildFeatures { buildConfig = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    testOptions { unitTests.isReturnDefaultValues = true }
}

dependencies {
    implementation("ai.pleos.playground:Vehicle:2.3.2")
    implementation("ai.pleos.playground:NaviHelper:3.0.5")
    implementation("ai.pleos.playground:TextToSpeech:3.3.1")
    implementation("ai.pleos.playground:SpeechToText:3.3.1")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    // QR for 오늘의 도로일기 (diary.ready view_url). Core only: pure Java, no camera/scanner code.
    implementation("com.google.zxing:core:3.5.3")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
