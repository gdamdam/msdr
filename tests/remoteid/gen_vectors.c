/*
 * Generates tests/remoteid/data/vectors.json: hex-encoded OpenDroneID
 * messages plus the decoded values msdr/remoteid/messages.py must produce,
 * independent of (and cross-checking) the ported Python decoder.
 *
 * Links opendroneid-core-c (Apache-2.0) at commit
 * 6484f26545d4f012682524e2d843fab0fbdc0b34:
 *   libopendroneid__opendroneid.c/.h, libopendroneid__wifi.c, libopendroneid__odid_wifi.h
 * (renamed here with their upstream path flattened by '__'; see
 * tests/remoteid/data/README.md for provenance and the regeneration command).
 *
 * Build (macOS; wifi.c assumes glibc's <byteswap.h>, shimmed here):
 *   cc -I<odid_dir> -I<shim_dir> -o /tmp/gen_vectors tests/remoteid/gen_vectors.c \
 *      <odid_dir>/libopendroneid__opendroneid.c <odid_dir>/libopendroneid__wifi.c -lm
 *   /tmp/gen_vectors > tests/remoteid/data/vectors.json
 */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "opendroneid.h"
#include "odid_wifi.h"

static int g_first_vector = 1;

static void hex_print(FILE *f, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++)
        fprintf(f, "%02x", data[i]);
}

static void begin_vector(const char *name, const char *type)
{
    if (!g_first_vector)
        printf(",\n");
    g_first_vector = 0;
    printf("    {\n      \"name\": \"%s\",\n      \"type\": \"%s\",\n", name, type);
}

static void hex_field(const char *field, const uint8_t *data, size_t len)
{
    printf("      \"%s\": \"", field);
    hex_print(stdout, data, len);
    printf("\",\n");
}

/* ---- Basic ID -------------------------------------------------------- */

static void gen_basic_id_serial(void)
{
    ODID_BasicID_data d;
    ODID_BasicID_encoded enc;
    odid_initBasicIDData(&d);
    d.UAType = ODID_UATYPE_HELICOPTER_OR_MULTIROTOR;
    d.IDType = ODID_IDTYPE_SERIAL_NUMBER;
    strncpy(d.UASID, "1596895037A58M", sizeof(d.UASID));
    encodeBasicIDMessage(&enc, &d);

    begin_vector("basic_id_serial", "message");
    hex_field("hex", (uint8_t *)&enc, ODID_MESSAGE_SIZE);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 0,\n");
    printf("        \"protocol_version\": %d,\n", ODID_PROTOCOL_VERSION);
    printf("        \"ua_type\": %d,\n", d.UAType);
    printf("        \"id_type\": %d,\n", d.IDType);
    printf("        \"uas_id\": \"%s\"\n", d.UASID);
    printf("      }\n    }");
}

static void gen_basic_id_utm_uuid(void)
{
    /* UTM-assigned UUID: binary 16-byte id in UASID[0:16], not a C string;
       opendroneid's decoder memcpy()s this ID type verbatim (no NUL strip). */
    ODID_BasicID_data d;
    ODID_BasicID_encoded enc;
    odid_initBasicIDData(&d);
    d.UAType = ODID_UATYPE_AEROPLANE;
    d.IDType = ODID_IDTYPE_UTM_ASSIGNED_UUID;
    uint8_t uuid[16] = {
        0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef,
        0xfe, 0xdc, 0xba, 0x98, 0x76, 0x54, 0x32, 0x10,
    };
    memcpy(d.UASID, uuid, sizeof(uuid));
    encodeBasicIDMessage(&enc, &d);

    begin_vector("basic_id_utm_uuid", "message");
    hex_field("hex", (uint8_t *)&enc, ODID_MESSAGE_SIZE);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 0,\n");
    printf("        \"ua_type\": %d,\n", d.UAType);
    printf("        \"id_type\": %d,\n", d.IDType);
    printf("        \"uas_id_hex\": \"");
    hex_print(stdout, (uint8_t *)d.UASID, ODID_ID_SIZE);
    printf("\"\n      }\n    }");
}

/* ---- Location ---------------------------------------------------------- */

static void gen_location(const char *name, const ODID_Location_data *in)
{
    ODID_Location_encoded enc;
    ODID_Location_data out;
    const ODID_Location_data *d = &out;
    encodeLocationMessage(&enc, in);
    /* Decode the round-tripped buffer back, rather than printing the
       pre-encode input: encoding quantizes (e.g. 215.7 deg -> nearest
       whole degree), and the vector must match what the C decoder itself
       produces from the wire bytes, not what was asked to be encoded. */
    decodeLocationMessage(&out, &enc);

    begin_vector(name, "message");
    hex_field("hex", (uint8_t *)&enc, ODID_MESSAGE_SIZE);
    int latlon_unknown = (d->Latitude == 0.0 && d->Longitude == 0.0);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 1,\n");
    printf("        \"status\": %d,\n", d->Status);
    if (d->Direction == INV_DIR)
        printf("        \"direction_deg\": null,\n");
    else
        printf("        \"direction_deg\": %.1f,\n", (double)d->Direction);
    if (d->SpeedHorizontal == INV_SPEED_H)
        printf("        \"speed_horizontal_ms\": null,\n");
    else
        printf("        \"speed_horizontal_ms\": %.3f,\n", (double)d->SpeedHorizontal);
    if (d->SpeedVertical == INV_SPEED_V)
        printf("        \"speed_vertical_ms\": null,\n");
    else
        printf("        \"speed_vertical_ms\": %.2f,\n", (double)d->SpeedVertical);
    if (latlon_unknown) {
        printf("        \"latitude\": null,\n");
        printf("        \"longitude\": null,\n");
    } else {
        printf("        \"latitude\": %.7f,\n", d->Latitude);
        printf("        \"longitude\": %.7f,\n", d->Longitude);
    }
    if (d->AltitudeBaro == INV_ALT)
        printf("        \"altitude_baro_m\": null,\n");
    else
        printf("        \"altitude_baro_m\": %.1f,\n", (double)d->AltitudeBaro);
    if (d->AltitudeGeo == INV_ALT)
        printf("        \"altitude_geo_m\": null,\n");
    else
        printf("        \"altitude_geo_m\": %.1f,\n", (double)d->AltitudeGeo);
    printf("        \"height_type\": %d,\n", d->HeightType);
    if (d->Height == INV_ALT)
        printf("        \"height_m\": null,\n");
    else
        printf("        \"height_m\": %.1f,\n", (double)d->Height);
    printf("        \"horiz_accuracy_enum\": %d,\n", d->HorizAccuracy);
    printf("        \"vert_accuracy_enum\": %d,\n", d->VertAccuracy);
    printf("        \"baro_accuracy_enum\": %d,\n", d->BaroAccuracy);
    printf("        \"speed_accuracy_enum\": %d,\n", d->SpeedAccuracy);
    printf("        \"ts_accuracy_enum\": %d,\n", d->TSAccuracy);
    if (d->TimeStamp == INV_TIMESTAMP)
        printf("        \"timestamp_s\": null\n");
    else
        printf("        \"timestamp_s\": %.1f\n", (double)d->TimeStamp);
    printf("      }\n    }");
}

/* ---- Auth ---------------------------------------------------------------*/

static void gen_auth_page0(void)
{
    ODID_Auth_data d;
    ODID_Auth_encoded enc;
    odid_initAuthData(&d);
    d.AuthType = ODID_AUTH_UAS_ID_SIGNATURE;
    d.DataPage = 0;
    d.LastPageIndex = 1;
    d.Length = 40;
    d.Timestamp = 28000000;
    const char data0[] = "12345678901234567";
    memcpy(d.AuthData, data0, sizeof(data0) - 1);
    encodeAuthMessage(&enc, &d);

    begin_vector("auth_page0", "message");
    hex_field("hex", (uint8_t *)&enc, ODID_MESSAGE_SIZE);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 2,\n");
    printf("        \"auth_type\": %d,\n", d.AuthType);
    printf("        \"data_page\": %d,\n", d.DataPage);
    printf("        \"last_page_index\": %d,\n", d.LastPageIndex);
    printf("        \"length\": %d,\n", d.Length);
    printf("        \"timestamp_s_since_2019\": %u,\n", d.Timestamp);
    printf("        \"auth_data_hex\": \"");
    hex_print(stdout, d.AuthData, ODID_AUTH_PAGE_ZERO_DATA_SIZE);
    printf("\"\n      }\n    }");
}

static void gen_auth_page1(void)
{
    ODID_Auth_data d;
    ODID_Auth_encoded enc;
    odid_initAuthData(&d);
    d.AuthType = ODID_AUTH_UAS_ID_SIGNATURE;
    d.DataPage = 1;
    const char data1[] = "12345678901234567890123";
    memcpy(d.AuthData, data1, sizeof(data1) - 1);
    encodeAuthMessage(&enc, &d);

    begin_vector("auth_page1", "message");
    hex_field("hex", (uint8_t *)&enc, ODID_MESSAGE_SIZE);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 2,\n");
    printf("        \"auth_type\": %d,\n", d.AuthType);
    printf("        \"data_page\": %d,\n", d.DataPage);
    printf("        \"auth_data_hex\": \"");
    hex_print(stdout, d.AuthData, ODID_AUTH_PAGE_NONZERO_DATA_SIZE);
    printf("\"\n      }\n    }");
}

/* ---- SelfID / System / OperatorID --------------------------------------*/

static void gen_self_id(void)
{
    ODID_SelfID_data d;
    ODID_SelfID_encoded enc;
    odid_initSelfIDData(&d);
    d.DescType = ODID_DESC_TYPE_TEXT;
    strncpy(d.Desc, "DronesRUS: Real Estate", sizeof(d.Desc));
    encodeSelfIDMessage(&enc, &d);

    begin_vector("self_id", "message");
    hex_field("hex", (uint8_t *)&enc, ODID_MESSAGE_SIZE);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 3,\n");
    printf("        \"desc_type\": %d,\n", d.DescType);
    printf("        \"description\": \"%s\"\n", d.Desc);
    printf("      }\n    }");
}

static void gen_system(const char *name, const ODID_System_data *in)
{
    ODID_System_encoded enc;
    ODID_System_data out;
    const ODID_System_data *d = &out;
    encodeSystemMessage(&enc, in);
    decodeSystemMessage(&out, &enc); /* see gen_location(): decode the round trip, not the input */

    begin_vector(name, "message");
    hex_field("hex", (uint8_t *)&enc, ODID_MESSAGE_SIZE);
    int latlon_unknown = (d->OperatorLatitude == 0.0 && d->OperatorLongitude == 0.0);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 4,\n");
    printf("        \"operator_location_type\": %d,\n", d->OperatorLocationType);
    printf("        \"classification_type\": %d,\n", d->ClassificationType);
    if (latlon_unknown) {
        printf("        \"operator_latitude\": null,\n");
        printf("        \"operator_longitude\": null,\n");
    } else {
        printf("        \"operator_latitude\": %.7f,\n", d->OperatorLatitude);
        printf("        \"operator_longitude\": %.7f,\n", d->OperatorLongitude);
    }
    printf("        \"area_count\": %d,\n", d->AreaCount);
    printf("        \"area_radius_m\": %d,\n", d->AreaRadius);
    if (d->AreaCeiling == INV_ALT)
        printf("        \"area_ceiling_m\": null,\n");
    else
        printf("        \"area_ceiling_m\": %.1f,\n", (double)d->AreaCeiling);
    if (d->AreaFloor == INV_ALT)
        printf("        \"area_floor_m\": null,\n");
    else
        printf("        \"area_floor_m\": %.1f,\n", (double)d->AreaFloor);
    printf("        \"category_eu\": %d,\n", d->CategoryEU);
    printf("        \"class_eu\": %d,\n", d->ClassEU);
    if (d->OperatorAltitudeGeo == INV_ALT)
        printf("        \"operator_altitude_geo_m\": null,\n");
    else
        printf("        \"operator_altitude_geo_m\": %.1f,\n", (double)d->OperatorAltitudeGeo);
    printf("        \"timestamp_s_since_2019\": %u\n", d->Timestamp);
    printf("      }\n    }");
}

static void gen_operator_id(void)
{
    ODID_OperatorID_data d;
    ODID_OperatorID_encoded enc;
    odid_initOperatorIDData(&d);
    d.OperatorIdType = ODID_OPERATOR_ID;
    strncpy(d.OperatorId, "98765432100123456789", sizeof(d.OperatorId));
    encodeOperatorIDMessage(&enc, &d);

    begin_vector("operator_id", "message");
    hex_field("hex", (uint8_t *)&enc, ODID_MESSAGE_SIZE);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 5,\n");
    printf("        \"operator_id_type\": %d,\n", d.OperatorIdType);
    printf("        \"operator_id\": \"%s\"\n", d.OperatorId);
    printf("      }\n    }");
}

/* ---- Message pack + beacon frame --------------------------------------*/

static void fill_full_uas_data(ODID_UAS_Data *uas)
{
    odid_initUasData(uas);

    uas->BasicID[0].UAType = ODID_UATYPE_HELICOPTER_OR_MULTIROTOR;
    uas->BasicID[0].IDType = ODID_IDTYPE_CAA_REGISTRATION_ID;
    strncpy(uas->BasicID[0].UASID, "12345678901234567890", sizeof(uas->BasicID[0].UASID));
    uas->BasicIDValid[0] = 1;

    uas->Location.Status = ODID_STATUS_AIRBORNE;
    uas->Location.Direction = 215.7f;
    uas->Location.SpeedHorizontal = 5.4f;
    uas->Location.SpeedVertical = 5.25f;
    uas->Location.Latitude = 45.539309;
    uas->Location.Longitude = -122.966389;
    uas->Location.AltitudeBaro = 100;
    uas->Location.AltitudeGeo = 110;
    uas->Location.HeightType = ODID_HEIGHT_REF_OVER_GROUND;
    uas->Location.Height = 80;
    uas->Location.HorizAccuracy = createEnumHorizontalAccuracy(2.5f);
    uas->Location.VertAccuracy = createEnumVerticalAccuracy(0.5f);
    uas->Location.BaroAccuracy = createEnumVerticalAccuracy(1.5f);
    uas->Location.SpeedAccuracy = createEnumSpeedAccuracy(0.5f);
    uas->Location.TSAccuracy = createEnumTimestampAccuracy(0.2f);
    uas->Location.TimeStamp = 360.5f;
    uas->LocationValid = 1;

    uas->Auth[0].AuthType = ODID_AUTH_UAS_ID_SIGNATURE;
    uas->Auth[0].DataPage = 0;
    uas->Auth[0].LastPageIndex = 1;
    uas->Auth[0].Length = 40;
    uas->Auth[0].Timestamp = 28000000;
    memcpy(uas->Auth[0].AuthData, "12345678901234567", 17);
    uas->AuthValid[0] = 1;

    uas->Auth[1].AuthType = ODID_AUTH_UAS_ID_SIGNATURE;
    uas->Auth[1].DataPage = 1;
    memcpy(uas->Auth[1].AuthData, "12345678901234567890123", 23);
    uas->AuthValid[1] = 1;

    uas->SelfID.DescType = ODID_DESC_TYPE_TEXT;
    strncpy(uas->SelfID.Desc, "DronesRUS: Real Estate", sizeof(uas->SelfID.Desc));
    uas->SelfIDValid = 1;

    uas->System.OperatorLocationType = ODID_OPERATOR_LOCATION_TYPE_TAKEOFF;
    uas->System.ClassificationType = ODID_CLASSIFICATION_TYPE_EU;
    uas->System.OperatorLatitude = uas->Location.Latitude + 0.00001;
    uas->System.OperatorLongitude = uas->Location.Longitude + 0.00001;
    uas->System.AreaCount = 35;
    uas->System.AreaRadius = 75;
    uas->System.AreaCeiling = 176.9f;
    uas->System.AreaFloor = 41.7f;
    uas->System.CategoryEU = ODID_CATEGORY_EU_SPECIFIC;
    uas->System.ClassEU = ODID_CLASS_EU_CLASS_3;
    uas->System.OperatorAltitudeGeo = 20.5f;
    uas->System.Timestamp = 28000000;
    uas->SystemValid = 1;

    uas->OperatorID.OperatorIdType = ODID_OPERATOR_ID;
    strncpy(uas->OperatorID.OperatorId, "98765432100123456789", sizeof(uas->OperatorID.OperatorId));
    uas->OperatorIDValid = 1;
}

static void gen_message_pack(void)
{
    ODID_UAS_Data uas;
    ODID_MessagePack_encoded pack_enc;
    ODID_MessagePack_data pack;
    uint8_t buf[2048];
    int ret;

    fill_full_uas_data(&uas);

    ret = odid_message_build_pack(&uas, buf, sizeof(buf));
    (void)pack_enc;
    (void)pack;

    begin_vector("message_pack_7", "message_pack");
    printf("      \"message_count\": 7,\n");
    hex_field("hex", buf, (size_t)ret);
    printf("      \"decoded\": {\n");
    printf("        \"message_type\": 15,\n");
    printf("        \"basic_id_uas_id\": \"%s\",\n", uas.BasicID[0].UASID);
    printf("        \"location_status\": %d,\n", uas.Location.Status);
    printf("        \"self_id_description\": \"%s\",\n", uas.SelfID.Desc);
    printf("        \"operator_id\": \"%s\"\n", uas.OperatorID.OperatorId);
    printf("      }\n    }");
}

static void gen_beacon_frame(void)
{
    ODID_UAS_Data uas;
    uint8_t buf[2048];
    uint8_t mac[6] = { 0x02, 0x11, 0x22, 0x33, 0x44, 0x55 };
    const char ssid[] = "MSDR_TEST_DRONE";
    int ret;

    fill_full_uas_data(&uas);

    ret = odid_wifi_build_message_pack_beacon_frame(
        &uas, (const char *)mac, ssid, sizeof(ssid) - 1, 100, 42, buf, sizeof(buf));

    begin_vector("wifi_beacon_message_pack", "wifi_beacon_frame");
    printf("      \"message_counter\": 42,\n");
    printf("      \"vendor_oui\": \"fa0bbc\",\n");
    printf("      \"vendor_oui_type\": 13,\n");
    hex_field("hex", buf, (size_t)ret);
    printf("      \"decoded\": {\n");
    printf("        \"basic_id_uas_id\": \"%s\",\n", uas.BasicID[0].UASID);
    printf("        \"location_status\": %d\n", uas.Location.Status);
    printf("      }\n    }");
}

int main(void)
{
    printf("{\n");
    printf("  \"commit\": \"6484f26545d4f012682524e2d843fab0fbdc0b34\",\n");
    printf("  \"vectors\": [\n");

    gen_basic_id_serial();
    gen_basic_id_utm_uuid();

    {
        ODID_Location_data d;
        odid_initLocationData(&d);
        d.Status = ODID_STATUS_AIRBORNE;
        d.Direction = 215.7f;
        d.SpeedHorizontal = 5.4f;
        d.SpeedVertical = 5.25f;
        d.Latitude = 45.539309;
        d.Longitude = -122.966389;
        d.AltitudeBaro = 100;
        d.AltitudeGeo = 110;
        d.HeightType = ODID_HEIGHT_REF_OVER_GROUND;
        d.Height = 80;
        d.HorizAccuracy = createEnumHorizontalAccuracy(2.5f);
        d.VertAccuracy = createEnumVerticalAccuracy(0.5f);
        d.BaroAccuracy = createEnumVerticalAccuracy(1.5f);
        d.SpeedAccuracy = createEnumSpeedAccuracy(0.5f);
        d.TSAccuracy = createEnumTimestampAccuracy(0.2f);
        d.TimeStamp = 360.5f;
        gen_location("location_normal", &d);
    }

    {
        /* Entirely unset/unknown: every optional field at its sentinel. */
        ODID_Location_data d;
        odid_initLocationData(&d);
        d.TimeStamp = (float)INV_TIMESTAMP;
        gen_location("location_unknown", &d);
    }

    {
        /* Boundary values: max lat/lon/alt/speed, direction wrap at 360->0,
           and the timestamp just before the hourly wrap. */
        ODID_Location_data d;
        odid_initLocationData(&d);
        d.Status = ODID_STATUS_EMERGENCY;
        d.Direction = 359.0f;
        d.SpeedHorizontal = MAX_SPEED_H;
        d.SpeedVertical = MAX_SPEED_V;
        d.Latitude = MAX_LAT;
        d.Longitude = MAX_LON;
        d.AltitudeBaro = MAX_ALT;
        d.AltitudeGeo = MIN_ALT; /* also the "unknown" sentinel value */
        d.HeightType = ODID_HEIGHT_REF_OVER_TAKEOFF;
        d.Height = 0;
        d.HorizAccuracy = ODID_HOR_ACC_1_METER;
        d.VertAccuracy = ODID_VER_ACC_1_METER;
        d.BaroAccuracy = ODID_VER_ACC_1_METER;
        d.SpeedAccuracy = ODID_SPEED_ACC_0_3_METERS_PER_SECOND;
        d.TSAccuracy = ODID_TIME_ACC_1_5_SECOND;
        d.TimeStamp = 3599.9f;
        gen_location("location_boundary", &d);
    }

    {
        ODID_Location_data d;
        odid_initLocationData(&d);
        d.Status = ODID_STATUS_GROUND;
        d.TimeStamp = 0.0f; /* valid: exactly the top of the hour */
        gen_location("location_timestamp_zero", &d);
    }

    gen_auth_page0();
    gen_auth_page1();
    gen_self_id();

    {
        ODID_System_data d;
        odid_initSystemData(&d);
        d.OperatorLocationType = ODID_OPERATOR_LOCATION_TYPE_TAKEOFF;
        d.ClassificationType = ODID_CLASSIFICATION_TYPE_EU;
        d.OperatorLatitude = 45.539319;
        d.OperatorLongitude = -122.966379;
        d.AreaCount = 35;
        d.AreaRadius = 75;
        d.AreaCeiling = 176.9f;
        d.AreaFloor = 41.7f;
        d.CategoryEU = ODID_CATEGORY_EU_SPECIFIC;
        d.ClassEU = ODID_CLASS_EU_CLASS_3;
        d.OperatorAltitudeGeo = 20.5f;
        d.Timestamp = 28000000;
        gen_system("system_normal", &d);
    }
    {
        ODID_System_data d;
        odid_initSystemData(&d); /* lat/lon left at 0,0: unknown */
        gen_system("system_unknown", &d);
    }

    gen_operator_id();
    gen_message_pack();
    gen_beacon_frame();

    printf("\n  ]\n}\n");
    return 0;
}
