PRESETS = {
    "low": {"guest_deploy_enabled": 1, "require_approval": 1, "max_apps_per_device": 1, "max_upload_mb": 5, "max_data_mb": 250, "max_memory_mb": 128, "max_cpu_percent": 10, "inactivity_minutes": 30, "allowed_runtimes": ["static", "python"]},
    "medium": {"guest_deploy_enabled": 1, "require_approval": 1, "max_apps_per_device": 2, "max_upload_mb": 10, "max_data_mb": 500, "max_memory_mb": 256, "max_cpu_percent": 25, "inactivity_minutes": 60, "allowed_runtimes": ["static", "python", "node"]},
    "high": {"guest_deploy_enabled": 1, "require_approval": 0, "max_apps_per_device": 4, "max_upload_mb": 10, "max_data_mb": 2048, "max_memory_mb": 512, "max_cpu_percent": 50, "inactivity_minutes": 0, "allowed_runtimes": ["static", "python", "node"]},
}
