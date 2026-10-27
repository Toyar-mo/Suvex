#include <bkcrack/Zip.hpp>

#include <TestRunner.hpp>

#include <filesystem>
#include <fstream>
#include <sstream>
#include <string_view>

namespace
{
const auto testFolder = std::filesystem::path{__FILE__}.replace_filename("data");
} // namespace

TEST("Zip::Error")
{
    const auto error = Zip::Error{"description"};
    CHECK(error.what() == std::string_view{"Zip error: description."});
}

TEST("parse empty.zip")
{
    auto ifs = std::ifstream{testFolder / "empty.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};
    CHECK(zip.begin() == zip.end());
}

TEST("fail to parse non-zip file")
{
    auto ifs = std::ifstream{testFolder / "make_test_data.sh", std::ios::binary};
    CHECK(ifs.is_open());

    CHECK_THROWS(Zip::Error, "could not find end of central directory signature", Zip{ifs});
}

TEST("parse plan.zip")
{
    auto ifs = std::ifstream{testFolder / "plain.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};
    auto       it  = zip.begin();
    const auto end = zip.end();

    CHECK(it != zip.end());
    auto entry = *it;
    CHECK(entry.name == "store.txt");
    CHECK(entry.encryption == Zip::Encryption::None);
    CHECK(entry.compression == Zip::Compression::Store);
    CHECK(entry.crc32 == 0x1ca08acd);
    CHECK(entry.offset == 0);
    CHECK(entry.packedSize == 208);
    CHECK(entry.uncompressedSize == 208);
    CHECK(entry.checkByte == 0x1c);

    CHECK(++it != zip.end());
    entry = *it;
    CHECK(entry.name == "deflate.txt");
    CHECK(entry.encryption == Zip::Encryption::None);
    CHECK(entry.compression == Zip::Compression::Deflate);
    CHECK(entry.crc32 == 0x45e207a8);
    CHECK(entry.offset == 247);
    CHECK(entry.packedSize == 71);
    CHECK(entry.uncompressedSize == 260);
    CHECK(entry.checkByte == 0x45);

    CHECK(++it == zip.end());
}

TEST("parse zipcrypto.zip")
{
    auto ifs = std::ifstream{testFolder / "zipcrypto.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};
    auto       it  = zip.begin();
    const auto end = zip.end();

    CHECK(it != zip.end());
    auto entry = *it;
    CHECK(entry.name == "store.txt");
    CHECK(entry.encryption == Zip::Encryption::Traditional);
    CHECK(entry.compression == Zip::Compression::Store);
    CHECK(entry.crc32 == 0x1ca08acd);
    CHECK(entry.offset == 0);
    CHECK(entry.packedSize == 220);
    CHECK(entry.uncompressedSize == 208);
    CHECK(entry.checkByte == 0xab);

    CHECK(++it != zip.end());
    entry = *it;
    CHECK(entry.name == "deflate.txt");
    CHECK(entry.encryption == Zip::Encryption::Traditional);
    CHECK(entry.compression == Zip::Compression::Deflate);
    CHECK(entry.crc32 == 0x45e207a8);
    CHECK(entry.offset == 275);
    CHECK(entry.packedSize == 83);
    CHECK(entry.uncompressedSize == 260);
    CHECK(entry.checkByte == 0xab);

    CHECK(++it == zip.end());
}

TEST("parse zip64.zip")
{
    auto ifs = std::ifstream{testFolder / "zip64.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};
    auto       it  = zip.begin();
    const auto end = zip.end();

    CHECK(it != zip.end());
    auto entry = *it;
    CHECK(entry.name == "store.txt");
    CHECK(entry.encryption == Zip::Encryption::None);
    CHECK(entry.compression == Zip::Compression::Store);
    CHECK(entry.crc32 == 0x1ca08acd);
    CHECK(entry.offset == 0);
    CHECK(entry.packedSize == 208);
    CHECK(entry.uncompressedSize == 208);
    CHECK(entry.checkByte == 0x1c);

    CHECK(++it != zip.end());
    entry = *it;
    CHECK(entry.name == "deflate.txt");
    CHECK(entry.encryption == Zip::Encryption::None);
    CHECK(entry.compression == Zip::Compression::Deflate);
    CHECK(entry.crc32 == 0x45e207a8);
    CHECK(entry.offset == 267);
    CHECK(entry.packedSize == 71);
    CHECK(entry.uncompressedSize == 260);
    CHECK(entry.checkByte == 0x45);

    CHECK(++it == zip.end());
}

TEST("parse zip64-zipcrypto.zip")
{
    auto ifs = std::ifstream{testFolder / "zip64-zipcrypto.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};
    auto       it  = zip.begin();
    const auto end = zip.end();

    CHECK(it != zip.end());
    auto entry = *it;
    CHECK(entry.name == "store.txt");
    CHECK(entry.encryption == Zip::Encryption::Traditional);
    CHECK(entry.compression == Zip::Compression::Store);
    CHECK(entry.crc32 == 0x1ca08acd);
    CHECK(entry.offset == 0);
    CHECK(entry.packedSize == 220);
    CHECK(entry.uncompressedSize == 208);
    CHECK(entry.checkByte == 0xab);

    CHECK(++it != zip.end());
    entry = *it;
    CHECK(entry.name == "deflate.txt");
    CHECK(entry.encryption == Zip::Encryption::Traditional);
    CHECK(entry.compression == Zip::Compression::Deflate);
    CHECK(entry.crc32 == 0x45e207a8);
    CHECK(entry.offset == 303);
    CHECK(entry.packedSize == 83);
    CHECK(entry.uncompressedSize == 260);
    CHECK(entry.checkByte == 0xab);

    CHECK(++it == zip.end());
}

TEST("parse aes256.zip")
{
    auto ifs = std::ifstream{testFolder / "aes256.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};
    auto       it  = zip.begin();
    const auto end = zip.end();

    CHECK(it != zip.end());
    auto entry = *it;
    CHECK(entry.name == "deflate.txt");
    CHECK(entry.encryption == Zip::Encryption::Unsupported);
    CHECK(entry.compression == Zip::Compression::Deflate);
    CHECK(entry.crc32 == 0x00000000);
    CHECK(entry.offset == 0);
    CHECK(entry.packedSize == 95);
    CHECK(entry.uncompressedSize == 260);
    CHECK(entry.checkByte == 0x00);

    CHECK(++it != zip.end());
    entry = *it;
    CHECK(entry.name == "store.txt");
    CHECK(entry.encryption == Zip::Encryption::Unsupported);
    CHECK(entry.compression == Zip::Compression::Store);
    CHECK(entry.crc32 == 0x00000000);
    CHECK(entry.offset == 147);
    CHECK(entry.packedSize == 236);
    CHECK(entry.uncompressedSize == 208);
    CHECK(entry.checkByte == 0x00);

    CHECK(++it == end);
}

TEST("get entry by name")
{
    auto ifs = std::ifstream{testFolder / "plain.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};

    const auto entryStore = zip["store.txt"];
    CHECK(entryStore.name == "store.txt");

    const auto entryDeflate = zip["deflate.txt"];
    CHECK(entryDeflate.name == "deflate.txt");

    CHECK_THROWS(Zip::Error, "Zip error: found no entry named \"does not exist\".", zip["does not exist"]);
}

TEST("get entry by index")
{
    auto ifs = std::ifstream{testFolder / "plain.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};

    const auto entry0 = zip[0];
    CHECK(entry0.name == "store.txt");

    const auto entry1 = zip[1];
    CHECK(entry1.name == "deflate.txt");

    CHECK_THROWS(Zip::Error, "Zip error: found no entry at index 2 (maximum index for this archive is 1).", zip[2]);
}

TEST("checkEncryption")
{
    auto entry = Zip::Entry{};
    entry.name = "test";

    entry.encryption = Zip::Encryption::None;
    Zip::checkEncryption(entry, Zip::Encryption::None);
    CHECK_THROWS(Zip::Error, "Zip error: entry \"test\" is not encrypted.",
                 Zip::checkEncryption(entry, Zip::Encryption::Traditional));

    entry.encryption = Zip::Encryption::Traditional;
    CHECK_THROWS(Zip::Error, "Zip error: entry \"test\" is encrypted.",
                 Zip::checkEncryption(entry, Zip::Encryption::None));
    Zip::checkEncryption(entry, Zip::Encryption::Traditional);

    entry.encryption = Zip::Encryption::Unsupported;
    CHECK_THROWS(Zip::Error, "Zip error: entry \"test\" is encrypted.",
                 Zip::checkEncryption(entry, Zip::Encryption::None));
    CHECK_THROWS(Zip::Error, "Zip error: entry \"test\" is encrypted with an unsupported algorithm.",
                 Zip::checkEncryption(entry, Zip::Encryption::Traditional));
}

TEST("seek to entry's data")
{
    auto ifs = std::ifstream{testFolder / "plain.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip   = Zip{ifs};
    const auto entry = zip["store.txt"];
    zip.seek(entry);
    CHECK(ifs.tellg() == 39);
    CHECK(ifs.get() == 's');
    CHECK(ifs.get() == 't');
    CHECK(ifs.get() == 'o');
    CHECK(ifs.get() == 'r');
    CHECK(ifs.get() == 'e');
    CHECK(ifs.get() == ' ');
    CHECK(ifs.get() == 'A');
}

TEST("load entry's data")
{
    auto ifs = std::ifstream{testFolder / "plain.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip   = Zip{ifs};
    const auto entry = zip["store.txt"];

    const auto data = zip.load(entry);
    CHECK(data.size() == 208);
    CHECK(data.front() == 's');
    CHECK(data.back() == '\n');

    const auto data5 = zip.load(entry, 7);
    CHECK(data5 == std::vector<std::uint8_t>{'s', 't', 'o', 'r', 'e', ' ', 'A'});
}

TEST("changeKeys")
{
    auto ifs = std::ifstream{testFolder / "zipcrypto.zip", std::ios::binary};
    CHECK(ifs.is_open());

    auto       progressOutput = std::ostringstream{};
    auto       progress       = Progress{progressOutput};
    auto       newZipStream   = std::stringstream{std::ios::in | std::ios::out | std::ios::binary};
    const auto zip            = Zip{ifs};
    zip.changeKeys(newZipStream, Keys{"password"}, Keys{"new_password"}, progress);
    CHECK(progress.done == 2);
    CHECK(progress.total == 2);
    CHECK(progressOutput.str().empty());

    const auto newZip     = Zip{newZipStream};
    const auto newStore   = newZip["store.txt"];
    const auto newDeflate = newZip["deflate.txt"];

    CHECK(newStore.name == "store.txt");
    CHECK(newStore.encryption == Zip::Encryption::Traditional);
    CHECK(newStore.compression == Zip::Compression::Store);
    CHECK(newStore.crc32 == 0x1ca08acd);
    CHECK(newStore.offset == 0);
    CHECK(newStore.packedSize == 220);
    CHECK(newStore.uncompressedSize == 208);
    CHECK(newStore.checkByte == 0xab);

    CHECK(newDeflate.name == "deflate.txt");
    CHECK(newDeflate.encryption == Zip::Encryption::Traditional);
    CHECK(newDeflate.compression == Zip::Compression::Deflate);
    CHECK(newDeflate.crc32 == 0x45e207a8);
    CHECK(newDeflate.offset == 275);
    CHECK(newDeflate.packedSize == 83);
    CHECK(newDeflate.uncompressedSize == 260);
    CHECK(newDeflate.checkByte == 0xab);

    newZip.seek(newStore);
    auto deciphered = std::ostringstream{std::ios::binary};
    decipher(newZipStream, 12 + 7, 11, deciphered, Keys{"new_password"});
    CHECK(deciphered.str() == "\xabstore A");
}

TEST("decrypt zipcrypto.zip")
{
    auto ifs = std::ifstream{testFolder / "zipcrypto.zip", std::ios::binary};
    CHECK(ifs.is_open());

    auto       progressOutput = std::ostringstream{};
    auto       progress       = Progress{progressOutput};
    auto       newZipStream   = std::stringstream{std::ios::in | std::ios::out | std::ios::binary};
    const auto zip            = Zip{ifs};
    zip.decrypt(newZipStream, Keys{"password"}, progress);
    CHECK(progress.done == 2);
    CHECK(progress.total == 2);
    CHECK(progressOutput.str().empty());

    const auto newZip     = Zip{newZipStream};
    const auto newStore   = newZip["store.txt"];
    const auto newDeflate = newZip["deflate.txt"];

    CHECK(newStore.name == "store.txt");
    CHECK(newStore.encryption == Zip::Encryption::None);
    CHECK(newStore.compression == Zip::Compression::Store);
    CHECK(newStore.crc32 == 0x1ca08acd);
    CHECK(newStore.offset == 0);
    CHECK(newStore.packedSize == 208);
    CHECK(newStore.uncompressedSize == 208);
    CHECK(newStore.checkByte == 0xab);

    CHECK(newDeflate.name == "deflate.txt");
    CHECK(newDeflate.encryption == Zip::Encryption::None);
    CHECK(newDeflate.compression == Zip::Compression::Deflate);
    CHECK(newDeflate.crc32 == 0x45e207a8);
    CHECK(newDeflate.offset == 263);
    CHECK(newDeflate.packedSize == 71);
    CHECK(newDeflate.uncompressedSize == 260);
    CHECK(newDeflate.checkByte == 0xab);

    CHECK(newZip.load(newStore, 7) == std::vector<std::uint8_t>{'s', 't', 'o', 'r', 'e', ' ', 'A'});
}

TEST("decrypt zip64-zipcrypto.zip")
{
    auto ifs = std::ifstream{testFolder / "zip64-zipcrypto.zip", std::ios::binary};
    CHECK(ifs.is_open());

    auto       progressOutput = std::ostringstream{};
    auto       progress       = Progress{progressOutput};
    auto       newZipStream   = std::stringstream{std::ios::in | std::ios::out | std::ios::binary};
    const auto zip            = Zip{ifs};
    zip.decrypt(newZipStream, Keys{"password"}, progress);
    CHECK(progress.done == 2);
    CHECK(progress.total == 2);
    CHECK(progressOutput.str().empty());

    const auto newZip     = Zip{newZipStream};
    const auto newStore   = newZip["store.txt"];
    const auto newDeflate = newZip["deflate.txt"];

    CHECK(newStore.name == "store.txt");
    CHECK(newStore.encryption == Zip::Encryption::None);
    CHECK(newStore.compression == Zip::Compression::Store);
    CHECK(newStore.crc32 == 0x1ca08acd);
    CHECK(newStore.offset == 0);
    CHECK(newStore.packedSize == 208);
    CHECK(newStore.uncompressedSize == 208);
    CHECK(newStore.checkByte == 0xab);

    CHECK(newDeflate.name == "deflate.txt");
    CHECK(newDeflate.encryption == Zip::Encryption::None);
    CHECK(newDeflate.compression == Zip::Compression::Deflate);
    CHECK(newDeflate.crc32 == 0x45e207a8);
    CHECK(newDeflate.offset == 291);
    CHECK(newDeflate.packedSize == 71);
    CHECK(newDeflate.uncompressedSize == 260);
    CHECK(newDeflate.checkByte == 0xab);

    CHECK(newZip.load(newStore, 7) == std::vector<std::uint8_t>{'s', 't', 'o', 'r', 'e', ' ', 'A'});
}

TEST("decrypt aes256.zip does nothing")
{
    auto ifs = std::ifstream{testFolder / "aes256.zip", std::ios::binary};
    CHECK(ifs.is_open());

    auto       progressOutput = std::ostringstream{};
    auto       progress       = Progress{progressOutput};
    auto       newZipStream   = std::stringstream{std::ios::in | std::ios::out | std::ios::binary};
    const auto zip            = Zip{ifs};
    zip.decrypt(newZipStream, Keys{"password"}, progress);
    CHECK(progress.done == 0);
    CHECK(progress.total == 0);
    CHECK(progressOutput.str().empty());

    ifs.seekg(0, std::ios::beg);
    CHECK(std::equal(std::istreambuf_iterator{ifs}, std::istreambuf_iterator<char>{},
                     std::istreambuf_iterator{newZipStream}, std::istreambuf_iterator<char>{}));
}

TEST("decipher")
{
    auto ifs = std::ifstream{testFolder / "zipcrypto.zip", std::ios::binary};
    CHECK(ifs.is_open());

    const auto zip = Zip{ifs};
    zip.seek(zip["store.txt"]);
    auto oss = std::ostringstream{std::ios::binary};
    decipher(ifs, 12 + 23, 12, oss, Keys{"password"});

    CHECK(oss.str() == "store A store B store C");
}
