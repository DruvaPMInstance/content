import demistomock as demisto
from CommonServerPython import *

import pyshark
import re
from typing import Dict, Any
import traceback
import pickle
import codecs

'''GLOBAL VARS'''
BAD_CHARS = ['[', ']', '>', '<', "'", ' Layer', ' ', '{', '}']
EMAIL_REGEX = r'\b[A-Za-z0-9._%=+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
IP_REGEX = r'\b(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.)' \
           r'{3}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\b'
URL_REGEX = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
PRAGMA_REGEX = r'Pragma: ([^\\]+)'
TYPE_REGEX = r'Type: (.+)'
CLASS_REGEX = r'Class: (.+)'
COMMAND_REGEX = r'Command: (.+)'
SNAME_REGEX = r'SNameString: (.+)'
MESSAGE_CODE = r'Message Code: (.+)'
RESPONSE_CODE = r'Response code: (.+)'


class PCAP():
    @logger
    def __init__(self, is_reg_extract: bool, extracted_protocols: list, homemade_regex: str, unique_ips: bool):
        """
        PCAP class. This class will contain all data and functions in order to mine the pcap.

        Args:
            is_reg_extract: Whether to extract regular expressions from the PCAP. URL, IP, Email
            extracted_protocols: A list of protocols to extract
            homemade_regex: A regex to extract from the PCAP
            unique_ips: Whether to export to context the unique source and dest IPs from the PCAP.
        """

        # setup data structures
        self.hierarchy = {}  # type: Dict[str, int]
        self.num_of_packets = 0
        self.tcp_streams = 0
        self.udp_streams = 0
        self.last_packet = 0
        self.bytes_transmitted = 0
        self.min_time = float('inf')
        self.max_time = -float('inf')
        self.conversations = {}  # type: Dict[tuple, Any]
        self.flows = {}  # type: Dict[tuple, Any]
        self.unique_source_ip = set([])  # type: set
        self.unique_dest_ip = set([])  # type: set
        self.ips_extracted = set([])  # type: set
        self.urls_extracted = set([])  # type: set
        self.emails_extracted = set([])  # type: set
        self.homemade_extracted = set([])  # type: set
        self.last_layer = set([])  # type: set
        self.irc_data = list()  # type: list
        self.protocol_data = dict()  # type: Dict[str, Any]
        self.extracted_protocols = extracted_protocols
        self.homemade_regex = homemade_regex
        self.unique_ips = unique_ips
        for protocol in extracted_protocols:
            self.protocol_data[protocol] = dict()

        # Regex compilation
        if 'LLMNR' in extracted_protocols:
            self.llmnr_type = re.compile('Type: (.*)\n')
            self.llmnr_class = re.compile('Class: (.*)\n')
            self.llmnr_dict = {}  # type: dict

        if is_reg_extract:
            self.reg_ip = re.compile(IP_REGEX)
            self.reg_email = re.compile(EMAIL_REGEX)
            self.reg_url = re.compile(URL_REGEX)

        if 'HTTP' in extracted_protocols:
            self.reg_pragma = re.compile(PRAGMA_REGEX)

        if 'ICMP' in extracted_protocols:
            self.icmp_data = set()  # type: set
        if 'DNS' in extracted_protocols or 'NETBIOS' in extracted_protocols or 'ICMP' in extracted_protocols:
            self.reg_type = re.compile(TYPE_REGEX)
        if 'NETBIOS' in extracted_protocols:
            self.reg_class = re.compile(CLASS_REGEX)
        if 'SMB2' in extracted_protocols:
            self.reg_cmd = re.compile(COMMAND_REGEX)
        if 'KERBEROS' in extracted_protocols:
            self.reg_sname = re.compile(SNAME_REGEX)
            self.kerb_data = set()  # type: set
        if 'SSH' in extracted_protocols:
            self.ssh_data = {
                'ClientProtocols': set(),
                'ServerProtocols': set(),
                'KeyExchangeMessageCode': set()
            }  # type: dict
            self.reg_message_code = re.compile(MESSAGE_CODE)
        if 'FTP' in extracted_protocols:
            self.reg_res_code = re.compile(RESPONSE_CODE)
        if 'TELNET' in extracted_protocols:
            self.telnet_data = set()  # type: set
            self.telnet_commands = set()  # type: set
        if homemade_regex:
            self.reg_homemade = re.compile(self.homemade_regex)

    @logger
    def repopulate_pcap_class(self, entry_id: str) -> None:
        """
        If script is called with `iterate` parameter then it is called in a playbook and will run again and again (has
        to be defined in the playbook to rerun) until the whole PCAP is analyzed.
        The data after each iteration will be kept in context and this method will take it from context and repopulate
        the pcap class according to the saved data.
        """
        context = demisto.context()
        pcap_saved_data = ''
        pcap_results = context.get('PcapResults')
        if not pcap_results:
            return
        if isinstance(pcap_results, dict):
            if pcap_results.get('EntryID') == entry_id:
                pcap_saved_data = pcap_results.get('temp_data')
        else:
            for pcap_analysis in pcap_results:
                if pcap_analysis.get('EntryID') == entry_id:
                    pcap_saved_data = pcap_analysis.get('temp_data')
                    break
        if not pcap_saved_data:
            # The entry id was not found. This means that this is the first run of the loop.
            return

        # repopulate data

        self.__dict__.update(pickle.loads(codecs.decode(pcap_saved_data.encode(), "base64")))

    @logger
    def save_pcap_to_context(self, entry_id, mined_packets):
        pickled_str = codecs.encode(pickle.dumps(self.__dict__), "base64").decode()
        pcap_saved_data = {'EntryID': entry_id,
                           'temp_data': pickled_str}
        ec = {'PcapResults(val.EntryID == obj.EntryID)': pcap_saved_data}
        return_outputs(f'Analyzed {mined_packets} packets', ec, {})

    @logger
    def extract_dns(self, packet):
        dns_layer = packet.dns
        temp_dns = {
            'ID': dns_layer.get('id'),
            'Request': dns_layer.get('qry_name'),
            'Response': dns_layer.get('a'),
            'Type': self.reg_type.findall(str(dns_layer))[0] if self.reg_type.findall(
                str(dns_layer)) else None
        }
        add_to_data(self.protocol_data['DNS'], temp_dns)

    @logger
    def extract_kerberos(self, packet):
        kerb_layer = packet.kerberos
        sname_results = self.reg_sname.findall(str(kerb_layer))
        self.kerb_data.update({
            'Realm': kerb_layer.get('realm'),
            'CName': kerb_layer.get('CNameString'),
            'SName': sname_results if sname_results else None,
        })

    @logger
    def extract_telnet(self, packet):
        telnet_layer = packet.telnet
        for message in telnet_layer._get_all_field_lines():
            if 'Data:' in message:
                self.telnet_data.add(message.lstrip('Data: '))
                continue
            if ':' not in message:
                self.telnet_commands.add(message.lstrip('\t').rstrip('\n'))

    @logger
    def extract_llmnr(self, packet):
        llmnr_layer = packet.llmnr
        llmnr_layer_string = str(llmnr_layer)
        query_type_results = self.llmnr_type.findall(llmnr_layer_string)
        query_class_results = self.llmnr_class.findall(llmnr_layer_string)
        llmnr_data = {
            'ID': llmnr_layer.get('dns_id'),
            'QueryType': None if len(query_type_results) == 0 else query_type_results[0],
            'QueryClass': None if len(query_class_results) == 0 else query_class_results[0],
            'QueryName': str(llmnr_layer.get('dns_qry_name')),
            'Questions': int(llmnr_layer.get('dns_count_queries'))
        }
        add_to_data(self.protocol_data['LLMNR'], llmnr_data)

    @logger
    def extract_syslog(self, packet):
        syslog_layer = packet.syslog
        syslog_data = {
            'ID': syslog_layer.get('msgid'),
            'Message': syslog_layer.get('msg'),
            'Hostname': syslog_layer.get('hostname'),
            'Timestamp': syslog_layer.get('timestamp')
        }
        add_to_data(self.protocol_data['SYSLOG'], syslog_data)

    @logger
    def extract_imf(self, packet):
        imf_layer = packet.imf
        imf_data = {
            'ID': imf_layer.get('Message-ID', -1),
            'To': imf_layer.get('to'),
            'From': imf_layer.get('from'),
            'Subject': imf_layer.get('subject'),
            'MimeVersion': imf_layer.get('mime-version')
        }
        add_to_data(self.protocol_data['SMTP'], imf_data)

    @logger
    def extract_smtp(self, packet):
        smtp_layer = packet.smtp
        parameters = smtp_layer.req_parameter.split(':')
        smtp_data = {'ID': packet.tcp.seq}
        if len(parameters) == 2:
            smtp_data[parameters[0].title()] = strip(parameters[1], ['<', '>'])
        add_to_data(self.protocol_data['SMTP'], smtp_data, packet.tcp.nxtseq)

    @logger
    def extract_smb(self, packet):
        smb_layer = packet.smb2
        command_results = self.reg_cmd.findall(str(smb_layer))
        smb_data = {
            'ID': smb_layer.get('sesid', -1),
            'UserName': smb_layer.get('ntlmssp_auth_username'),
            'Domain': smb_layer.get('ntlmssp_auth_domain'),
            'HostName': smb_layer.get('ntlmssp_auth_hostname'),
            'Command': command_results[0] if command_results else None,
            'FileName': smb_layer.get('smb2.filename'),
            'Tree': smb_layer.get('tree')
        }
        add_to_data(self.protocol_data['SMB2'], smb_data)

    @logger
    def extract_netbios(self, packet):
        netbios_layer = packet.nbns
        type_results = self.reg_type.findall(str(netbios_layer))
        class_results = self.reg_class.findall(str(netbios_layer))
        netbios_data = {
            'ID': netbios_layer.get('id', -1),
            'Name': netbios_layer.get('name'),
            'Type': type_results if type_results else None,
            'Class': class_results if class_results else None
        }
        add_to_data(self.protocol_data['NETBIOS'], netbios_data)

    @logger
    def extract_icmp(self, packet):
        icmp_layer = packet.icmp
        type_results = self.reg_type.findall(str(icmp_layer))
        for result in type_results:
            self.icmp_data.add(result)

    @logger
    def extract_ssh(self, packet):
        ssh_layer = packet.ssh
        protocol = ssh_layer.get('protocol')
        message_code_results = self.reg_message_code.findall(str(ssh_layer))
        if protocol and ssh_layer.get('direction') == 1:
            # direction is server to client
            self.ssh_data['ServerProtocols'].add(protocol)
        if protocol and ssh_layer.get('direction') == 0:
            # direction is client to server
            self.ssh_data['ClientProtocols'].add(protocol)
        if message_code_results:
            if message_code_results:
                self.ssh_data['KeyExchangeMessageCode'].add(message_code_results[0])
        return

    @logger
    def extract_irc(self, packet):
        irc_layer = packet.irc
        if irc_layer.get('request'):
            command = irc_layer.get('request_command')
            trailer = irc_layer.get('request_trailer', '')
            prefix = irc_layer.get('request_prefix', '')
            parameters = irc_layer.get('request').replace(command, '').replace(trailer, '') \
                .replace(prefix, '').split(' ')
            parameters.remove(' ')
            irc_data = {
                'ID': packet.tcp.get('ack'),
                'RequestCommand': command,
                'RequestTrailer': trailer,
                'RequestPrefix': prefix,
                'RequestParameters': parameters
            }
        else:
            command = irc_layer.get('response_command')
            trailer = irc_layer.get('response_trailer', '')
            prefix = irc_layer.get('response_prefix', '')
            parameters = irc_layer.get('response').replace(command, '').replace(trailer, '') \
                .replace(prefix, '').split(' ')
            parameters.remove(' ')
            irc_data = {
                'ID': packet.tcp.get('seq'),
                'ResponseCommand': command,
                'ResponseTrailer': trailer,
                'ResponsePrefix': prefix,
                'ResponseParameters': parameters
            }
        add_to_data(self.protocol_data['IRC'], irc_data, next_id=packet.tcp.nxtseq)

    @logger
    def extract_ftp(self, packet):
        ftp_layer = packet.ftp
        res_code_results = self.reg_res_code.findall(str(ftp_layer))
        ftp_data = {
            'ID': packet.tcp.get('seq'),
            'RequestCommand': ftp_layer.get('request_command'),
            'ResponseArgs': ftp_layer.get('response_arg'),
            'ResponseCode': res_code_results[0] if res_code_results else None
        }
        if ftp_data['ResponseCode']:
            # if packet is a response, don't update ID. This is because FTP may have a lot of requests and
            # responses in the same conversation
            add_to_data(self.protocol_data['FTP'], ftp_data)
        else:
            add_to_data(self.protocol_data['FTP'], ftp_data, next_id=packet.tcp.ack)

    @logger
    def extract_context_from_packet(self, packet, layers_str: str, is_reg_extract: bool = False) -> None:
        """
        Get a packet and extract context from it for the specified protocols specified in `self.extracted_protocols`
        Args:
            packet: The packet itself.
            layers_str: A comma-seperated string of the layers in the packet.
            is_reg_extract: Whether to export to context the unique source and dest IPs from the PCAP.
        """

        layers = layers_str.split(',')
        if is_reg_extract:
            self.ips_extracted.update(self.reg_ip.findall(str(packet)))
            self.emails_extracted.update(self.reg_email.findall(str(packet)))
            self.urls_extracted.update(self.reg_url.findall(str(packet)))

        if self.homemade_regex:
            self.homemade_extracted.update(self.reg_homemade.findall((str(packet))))

        if 'DNS' in self.extracted_protocols and 'DNS' in layers:
            return self.extract_dns(packet)

        if 'KERBEROS' in self.extracted_protocols and 'KERBEROS' in layers:
            return self.extract_kerberos(packet)

        if 'TELNET' in self.extracted_protocols and 'TELNET' in layers:
            return self.extract_telnet(packet)

        if 'LLMNR' in self.extracted_protocols and 'LLMNR' in layers:
            return self.extract_llmnr(packet)

        if 'SYSLOG' in self.extracted_protocols and 'SYSLOG' in layers:
            return self.extract_syslog(packet)

        if 'SMB2' in self.extracted_protocols and 'SMB2' in layers:
            return self.extract_smb(packet)

        if 'NETBIOS' in self.extracted_protocols and 'NETBIOS' in layers:
            return self.extract_netbios(packet)

        if 'ICMP' in self.extracted_protocols and 'ICMP' in layers:
            return self.extract_icmp(packet)

        if 'SSH' in self.extracted_protocols and 'SSH' in layers:
            return self.extract_ssh(packet)

        if 'IRC' in self.extracted_protocols and 'IRC' in layers:
            return self.extract_irc(packet)

        if 'FTP' in self.extracted_protocols and 'FTP' in layers:
            return self.extract_ftp(packet)

        if 'SMTP' in self.extracted_protocols:
            if 'IMF' in layers:
                return self.extract_imf(packet)

            if 'SMTP' in layers:
                return self.extract_smtp(packet)

    @logger
    def get_outputs(self, entry_id, conversation_number_to_display=15, is_flows=False, is_reg_extract=False):
        if self.num_of_packets == 0:
            return "## No packets found.\nTry changing the filter, if applied.", {}, {}
        md = f'## PCAP Info:\n' \
            f'Between {formatEpochDate(self.min_time)} and {formatEpochDate(self.max_time)} ' \
            f'there were {self.num_of_packets} ' \
            f'packets transmitted in {self.tcp_streams + self.udp_streams} streams.\n'
        md += '#### Protocol Breakdown\n'
        md += hierarchy_to_md(self.hierarchy)
        md += f'#### Top {conversation_number_to_display} Conversations\n'
        md += conversations_to_md(self.conversations, conversation_number_to_display)
        if is_flows:
            md += f'#### Top {conversation_number_to_display} Flows\n'
            md += flows_to_md(self.flows, conversation_number_to_display)

        # Entry Context
        general_context = {
            'EntryID': entry_id,
            'Bytes': self.bytes_transmitted,
            'Packets': self.num_of_packets,
            'StreamCount': self.tcp_streams + self.udp_streams,
            'UniqueSourceIP': len(self.unique_source_ip),
            'UniqueDestIP': len(self.unique_dest_ip),
            'StartTime': formatEpochDate(self.min_time),
            'EndTime': formatEpochDate(self.max_time),
            'Protocols': list(self.last_layer)
        }
        for protocol in self.extracted_protocols:
            general_context[protocol] = list(self.protocol_data[protocol].values())
        if 'ICMP' in self.extracted_protocols:
            general_context['ICMP'] = list(self.icmp_data)
        if 'KERBEROS' in self.extracted_protocols:
            general_context['KERBEROS'] = list(self.kerb_data)
        if 'SSH' in self.extracted_protocols:
            general_context['SSH'] = dict()
            for key in self.ssh_data.keys():
                general_context['SSH'][key] = list(self.ssh_data[key])
        if 'TELNET' in self.extracted_protocols:
            general_context['Telnet'] = {'Commands': list(self.telnet_commands),
                                         'Data': list(self.telnet_data)}
        if is_flows:
            general_context['Flow'] = flows_to_ec(self.flows)
        if is_reg_extract:
            general_context['IP'] = list(self.ips_extracted)
            general_context['URL'] = list(self.urls_extracted)
            general_context['Email'] = list(self.emails_extracted)
        if self.homemade_regex:
            general_context['Regex'] = list(self.homemade_extracted)
        if self.unique_ips:
            all_ips = self.unique_source_ip.copy()
            all_ips.update(self.unique_dest_ip)
            if general_context.get('IP'):
                general_context.get('IP').append(list(all_ips))  # type: ignore
            else:
                general_context['IP'] = list(all_ips)

            general_context['SourceIP'] = list(self.unique_source_ip)
            general_context['DestIP'] = list(self.unique_dest_ip)
        ec = {'PcapResults(val.EntryID == obj.EntryID)': general_context}
        return md, ec, general_context

    @logger
    def mine(self, file_path: str, decrypt_key: str, is_flows: bool, is_reg_extract: bool, pcap_filter: str,
             pcap_filter_new_file_path: str, num_of_packets_to_mine=None) -> int:
        """
        The main function of the script. Mines the PCAP.

        Args:
            file_path: The PCAP's file path.
            decrypt_key: The decryption key (if needed) to decrypt the PCAP.
            is_flows: Whether to extract flows.
            is_reg_extract: Whether to extract regexes from the PCAP.
            pcap_filter: A filter to apply on the PCAP. Same filter syntax as in Wireshark
            pcap_filter_new_file_path: The new path to save the filtered PCAP in

        """

        try:
            cap = pyshark.FileCapture(file_path, display_filter=pcap_filter, output_file=pcap_filter_new_file_path,
                                      decryption_key=decrypt_key, encryption_type='WPA-PWD', keep_packets=False)
            j=0
            for packet in cap:
                j += 1

                if num_of_packets_to_mine and j == num_of_packets_to_mine:
                    break
                self.last_packet = int(packet.number)
                self.last_layer.add(packet.layers[-1].layer_name)

                layers = str(packet.layers)
                # remove duplicate layer names such as [ETH,DATA,DATA] -> # [ETH, DATA]
                layers = list(dict.fromkeys(layers.split(',')))  # type: ignore
                layers = strip(str(layers))
                self.hierarchy[layers] = self.hierarchy.get(layers, 0) + 1  # type: ignore

                # update times
                packet_epoch_time = float(packet.frame_info.get('time_epoch'))
                self.max_time = max(self.max_time, packet_epoch_time)
                self.min_time = min(self.min_time, packet_epoch_time)

                # count packets
                self.num_of_packets += 1

                # count bytes
                self.bytes_transmitted += int(packet.length)

                # count num of streams + get src/dest ports
                tcp = packet.get_multiple_layers('tcp')

                if tcp:
                    tcp_streams = max(int(tcp[0].get('stream', 0)), self.tcp_streams)
                    src_port = int(tcp[0].get('srcport', 0))
                    dest_port = int(tcp[0].get('dstport', 0))

                udp = packet.get_multiple_layers('udp')
                if udp:
                    udp_streams = max(int(udp[0].get('stream', 0)), self.udp_streams)
                    src_port = int(udp[0].get('srcport', 0))
                    dest_port = int(udp[0].get('dstport', 0))

                # add conversations
                ip_layer = packet.get_multiple_layers('ip')
                if ip_layer:
                    a = ip_layer[0].get('src_host', '')
                    b = ip_layer[0].get('dst_host')
                    self.unique_source_ip.add(a)
                    self.unique_dest_ip.add(b)

                    # generate flow data
                    if is_flows:
                        if "src_port" not in locals():
                            continue
                        if (b, dest_port, a, src_port) in self.flows.keys():
                            b, a, src_port, dest_port = a, b, dest_port, src_port
                        flow = (a, src_port, b, dest_port)
                        flow_data = self.flows.get(flow, {'min_time': float('inf'),
                                                          'max_time': -float('inf'),
                                                          'bytes': 0,
                                                          'counter': 0})
                        flow_data['min_time'] = min(flow_data['min_time'], packet_epoch_time)
                        flow_data['max_time'] = max(flow_data['min_time'], packet_epoch_time)
                        flow_data['bytes'] += int(packet.length)
                        flow_data['counter'] += 1
                        self.flows[flow] = flow_data

                    # gather http data
                    if 'HTTP' in self.extracted_protocols:
                        http_layer = packet.get_multiple_layers('http')
                        if http_layer:
                            http_layer = http_layer[0]
                            all_fields = http_layer._all_fields
                            temp_http = {
                                "ID": http_layer.get('request_in', packet.number),
                                'RequestAgent': all_fields.get("http.user_agent"),
                                'RequestHost': all_fields.get('http.host'),
                                'RequestSourceIP': a,
                                'RequestURI': http_layer.get('request_full_uri'),
                                'RequestMethod': http_layer.get('request_method'),
                                'RequestVersion': http_layer.get('request_version'),
                                'RequestAcceptEncoding': http_layer.get('accept_encoding'),
                                'RequestPragma': self.reg_pragma.findall(str(http_layer))[0]
                                if self.reg_pragma.findall(str(http_layer)) else None,
                                'RequestAcceptLanguage': http_layer.get('accept_language'),
                                'RequestCacheControl': http_layer.get('cache_control')

                            }
                            # if the packet is a response
                            if all_fields.get('http.response'):
                                temp_http.update({
                                    'ResponseStatusCode': http_layer.get('response_code'),
                                    'ResponseVersion': all_fields.get('http.response.version'),
                                    'ResponseCodeDesc': http_layer.get('response_code_desc'),
                                    'ResponseContentLength': http_layer.get('content_length'),
                                    'ResponseContentType': http_layer.get('content_type'),
                                    'ResponseDate': formatEpochDate(packet_epoch_time)
                                })
                            add_to_data(self.protocol_data['HTTP'], temp_http)
                    if (b, a) in self.conversations.keys():
                        a, b = b, a
                    hosts = (a, b)
                    self.conversations[hosts] = self.conversations.get(hosts, 0) + 1

                self.extract_context_from_packet(packet, layers, is_reg_extract)

            cap.close()
            return j

        except pyshark.capture.capture.TSharkCrashException:
            raise ValueError("Could not find packets. Make sure that the file is a .cap/.pcap/.pcapng file "
                             "or that filter is of the correct syntax.")


'''HELPER FUNCTIONS'''


@logger
def strip(s: str, bad_chars=None):
    """

    Args:
        s: string to strip
        bad_chars: all characters to remove from string.

    Returns:
        The input s without the bad_chars
    """
    if bad_chars is None:
        bad_chars = BAD_CHARS
    temp = s
    for char in bad_chars:
        temp = temp.replace(char, '')
    return temp


@logger
def hierarchy_to_md(hierarchy: dict) -> str:
    """

    Args:
        hierarchy: a dictionary of layer hierarchy for all packets

    Returns:
        A markdown string for displaying the hierarchy in a nice view. The script also counts the number of occurrences
        each hierarchy.
    """
    final_dict = {}  # type: Dict[str, Any]
    num_of_all_packets = 0
    for k in hierarchy.keys():
        layer_heir = ''
        for layer in k.split(','):
            layer_heir += ' -> ' + layer
            final_dict[layer_heir] = final_dict.get(layer_heir, 0) + hierarchy[k]
        num_of_all_packets += hierarchy[k]
    md = '|Layer| # of Packets|% of Packets|\n|---|----|---|\n'
    for key in sorted(final_dict):
        md += f'|{key}|{final_dict[key]}|{round(final_dict[key] / num_of_all_packets, 3) * 100}%|\n'
    return md


@logger
def conversations_to_md(conversations: dict, disp_num: int) -> str:
    """

    Args:
        conversations: a raw dictionary of conversations.
        disp_num: The limit of conversations to display.

    Returns:
        A mardkown of <=disp_num of conversations, ordered in descending order.
    """
    md = '|A|B|# of Packets|\n|---|---|---|\n'
    ordered_conv_list = sorted(conversations.items(), key=lambda x: x[1], reverse=True)
    disp_num = min(disp_num, len(ordered_conv_list))
    for conv in ordered_conv_list[:disp_num]:
        (ipA, ipB), data = conv
        md += f'|{ipA}|{ipB}|{data}|\n'
    return md


@logger
def flows_to_md(flows: dict, disp_num: int) -> str:
    """

    Args:
        flows: a raw dictionary of flows.
        disp_num: The limit of flows to display.

    Returns:
        A mardkown of <=disp_num of flows, ordered in descending order.

    """
    md = '|A|port|B|port|# of Packets|\n|---|---|---|---|---|\n'
    ordered_flow_list = sorted(flows.items(), key=lambda x: x[1].get('counter'), reverse=True)
    disp_num = min(disp_num, len(ordered_flow_list))
    for flow in ordered_flow_list[:disp_num]:
        (ipA, portA, ipB, portB), data = flow
        md += f'|{ipA}|{portA}|{ipB}|{portB}|{data.get("counter", 0)}|\n'
    return md


@logger
def flows_to_ec(flows: dict) -> list:
    """

    Args:
        flows: A dictionary that hold the flows data

    Returns:
        flows data in ec format.
    """
    flows_ec = []
    for flow in flows.keys():
        flow_data = flows[flow]
        flow_ec = {
            'SourceIP': flow[0],
            'SourcePort': flow[1],
            'DestIP': flow[2],
            'DestPort': flow[3],
            'Duration': round(flow_data.get('max_time', 0) - flow_data.get('min_time', 0)),
            'StartTime': formatEpochDate(flow_data.get('min_time', 0)),
            'EndTime': formatEpochDate(flow_data.get('max_time', 0)),
            'Bytes': flow_data.get('bytes', 0)
        }
        flows_ec.append(flow_ec)
    return flows_ec


@logger
def add_to_data(d: dict, data: dict, next_id: int = None) -> None:
    """
    updates dictionary d to include/update the data. Also removes None values.
    Args:
        d: a Dictionary of ID: data to which we want to update the data according to ID
        data: the data to update. data must have an "ID" field.
        prev_id: when there's no ID in a packet, we'd like to keep link the data by the next sequence number from TCP
            protocol. In order to keep this up to date we'd like to know what the next sequence ID and then update
            it to be the current acknowledge number.

    Returns:
        updates dictionary d to include/update the data. Also removes None values.
    """
    data_id = data.get('ID')
    if not data_id:
        return
    else:
        if not d.get(data_id):
            if len(data.keys()) == 1:
                # The dictionary doesn't exist and is empty (except ID)
                return
            if next_id:
                d[next_id] = assign_params(**data)
            else:
                d[data_id] = assign_params(**data)
        else:
            if next_id and next_id > data_id:
                # id exists but we want to keep its next seq as ID (and also next seq id is larger therefore
                # there's another packet in the future)
                temp = d.pop(data_id)
                temp.update(assign_params(**data))
                d[next_id] = temp
            else:
                # ID exists, we just want to update it.
                d[data_id].update(assign_params(**data))


'''MAIN'''

def main():
    args = demisto.args()
    entry_id = args.get('entry_id', '')
    file_path = demisto.getFilePath(entry_id).get('path')
    # file_path = file_path[0]["Contents"]["path"]

    decrypt_key = args.get('wpa_password', '')

    decrypt_key_entry_id = args.get('rsa_decrypt_key_entry_id', '')
    if decrypt_key_entry_id and not decrypt_key:
        decrypt_key = demisto.getFilePath(entry_id).get('path')

    conversation_number_to_display = int(args.get('convs_to_display', '15'))
    extracted_protocols = argToList(args.get('protocol_output', ''))
    is_flows = True
    is_reg_extract = args.get('extract_strings', 'False') == 'True'
    pcap_filter = args.get('pcap_filter', '')
    homemade_regex = args.get('custom_regex', '')  # 'Layer (.+):'
    pcap_filter_new_file_path = ''
    pcap_filter_new_file_name = args.get('filtered_file_name', '')
    unique_ips = args.get('extract_ips', 'False') == 'True'

    if pcap_filter_new_file_name:
        temp = demisto.uniqueFile()
        pcap_filter_new_file_path = demisto.investigation()['id'] + '_' + temp

    # `iterate` argument and `loop packet size` are arguments used in order to enable looping over large PCAPs
    iterate = args.get('iterate', 'False') == 'True'
    packets_to_analyze = int(args.get('packets_to_analyze', 0))

    if iterate and not packets_to_analyze:
        return_error('In order to iterate over this pcap please specify the iteration size in `packets_to_analyze.'
                     ' These parameters are meant for playbook use.')

    try:
        pcap = PCAP(is_reg_extract, extracted_protocols, homemade_regex, unique_ips)
        if iterate:
            pcap.repopulate_pcap_class(entry_id)
            if pcap.last_packet > 0:
                if pcap_filter:
                    pcap_filter += f' && frame.number>{pcap.last_packet}'
                else:
                    pcap_filter = f'frame.number>{pcap.last_packet}'

        mined_packets = pcap.mine(file_path, decrypt_key, is_flows, is_reg_extract, pcap_filter,
                                  pcap_filter_new_file_path, packets_to_analyze)

        if iterate and mined_packets == packets_to_analyze:
            pcap.save_pcap_to_context(entry_id, mined_packets)

        else:
            hr, ec, raw = pcap.get_outputs(entry_id, conversation_number_to_display, is_flows, is_reg_extract)
            return_outputs(hr, ec, raw)

    except Exception as e:
        return_error(f'Unexpected error: {str(e)}', error=traceback.format_exc())

    if pcap_filter_new_file_name:
        demisto.results({'Contents': '', 'ContentsFormat': formats['text'], 'Type': 3,
                         'File': pcap_filter_new_file_name, 'FileID': temp})


def local_main():  # TODO remove this function
    # Variables from demisto
    # file_path = "/Users/olichter/Downloads/chargen-udp.pcap"
    # file_path = "/Users/olichter/Downloads/http-site.pcap"                 # HTTP
    # file_path = "/Users/olichter/Downloads/dns.cap"                        # DNS
    # file_path = "/Users/olichter/Downloads/tftp_rrq.pcap"                 # tftp
    file_path = "/Users/olichter/Downloads/rsasnakeoil2.cap"              # encrypted SSL
    # file_path = "/Users/olichter/Downloads/smb-legacy-implementation.pcapng"  # llmnr/netbios/smb
    # file_path = "/Users/olichter/Downloads/smtp.pcap"                      # SMTP#
    # file_path = "/Users/olichter/Downloads/nb6-hotspot.pcap"                #syslog
    # file_path = "/Users/olichter/Downloads/wpa-Induction.pcap"               #wpa - Password is Induction
    # file_path = "/Users/olichter/Downloads/iseries.cap"
    # file_path = "/Users/olichter/Downloads/2019-12-03-traffic-analysis-exercise.pcap"  # 1 min
    # file_path = "/Users/olichter/Downloads/smb-on-windows-10.pcapng"  # ran for 2.906 secs
    # file_path = "/Users/olichter/Downloads/telnet-cooked.pcap"                  # telnet
    # file_path = "/Users/olichter/Downloads/SSHv2.cap"                        #SSH
    # file_path = "/Users/olichter/Downloads/SkypeIRC.cap"                        #IRC
    # file_path = "/Users/olichter/Downloads/ftp.pcapng"                      #FTP
    # PC Script
    entry_id = ''

    decrypt_key = "Induction"  # "/Users/olichter/Downloads/rsasnakeoil2.key"
    conversation_number_to_display = 15
    is_flows = True
    is_reg_extract = True
    extracted_protocols = ['SMTP', 'DNS', 'HTTP', 'SMB2', 'NETBIOS', 'ICMP', 'KERBEROS', 'SYSLOG', 'SSH',
                           'IRC', 'FTP', 'TELNET', 'LLMNR']

    pcap_filter = ''
    homemade_regex = ''
    pcap_filter_new_file_path = ''
    unique_ips = True
    iterate = True
    pcap_saved_data = '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'

    pcap = PCAP(is_reg_extract, extracted_protocols, homemade_regex, unique_ips)
    pcap.__dict__.update(pickle.loads(codecs.decode(pcap_saved_data.encode(), "base64")))
    pcap_filter = f'frame.number>{pcap.last_packet}'
    pcap.mine(file_path, decrypt_key, is_flows, is_reg_extract, pcap_filter, pcap_filter_new_file_path, 30)
    hr, ec, raw = pcap.get_outputs(entry_id, conversation_number_to_display, is_flows, is_reg_extract)
    return_outputs(hr, ec, raw)


if __name__ in ['__main__', 'builtin', 'builtins']:
    # local_main()
    main()
