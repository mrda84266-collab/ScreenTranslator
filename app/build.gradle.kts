plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "ir.selfdev.app"
    compileSdk = 34
    defaultConfig {
        applicationId = "ir.selfdev.app"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        ndk { abiFilters += "arm64-v8a" }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    // llama-server به شکل libllama_server.so بسته‌بندی می‌شود و باید روی دیسک استخراج شود تا قابل اجرا باشد
    packaging { jniLibs { useLegacyPackaging = true } }
}

chaquopy {
    defaultConfig { version = "3.12" }
}
