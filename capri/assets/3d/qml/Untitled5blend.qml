import QtQuick
import QtQuick3D

Node {
    id: root
    objectName: "Root"

    // Resources
    property url textureData: "maps/textureData.png"
    Texture {
        id: _0_texture
        generateMipmaps: true
        mipFilter: Texture.Linear
        source: root.textureData
    }
    PrincipledMaterial {
        id: main_material
        objectName: "Main"
        baseColorMap: _0_texture
        roughness: 0.8999999761581421
        cullMode: PrincipledMaterial.NoCulling
        alphaMode: PrincipledMaterial.Opaque
        lighting: PrincipledMaterial.NoLighting
    }
    PrincipledMaterial {
        id: principledMaterial_material
        objectName: "PrincipledMaterial"
        metalness: 1
        roughness: 1
        alphaMode: PrincipledMaterial.Opaque
    }
    PrincipledMaterial {
        id: paper_Outline_Black_material
        objectName: "Paper_Outline_Black"
        baseColor: "#ff000000"
        metalness: 1
        roughness: 1
        emissiveFactor: Qt.vector3d(0.48082, 0, 1)
        alphaMode: PrincipledMaterial.Opaque
    }

    // Nodes:
    Model {
        id: plane_004
        objectName: "Plane.004"
        source: "meshes/plane_004_mesh.mesh"
        materials: [
            main_material,
            principledMaterial_material,
            paper_Outline_Black_material
        ]
    }

    // Animations:
}
