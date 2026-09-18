def classFactory(iface):
    from .plugin import SaveTempLayersPlugin
    return SaveTempLayersPlugin(iface)
