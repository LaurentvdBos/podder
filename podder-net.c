#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/if.h>
#include <linux/rtnetlink.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <arpa/inet.h>

int sendnl(int fd, struct nlmsghdr *hdr)
{
    static int seq = 0;

    struct iovec iov = { hdr, hdr->nlmsg_len };
    struct sockaddr_nl sa;
    struct msghdr msg = { &sa, sizeof(sa), &iov, 1, NULL, 0, 0 };

    memset(&sa, 0, sizeof(sa));
    sa.nl_family = AF_NETLINK;

    hdr->nlmsg_pid = 0;
    hdr->nlmsg_seq = seq++;

    return sendmsg(fd, &msg, 0);
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "Usage: %s <interface> <pid> <mac>\n\n", argv[0]);
        fprintf(stderr, "Here:\n");
        fprintf(stderr, "- interface: the interface to use for the macvlan.");
        fprintf(stderr, "- pid: process in the namespace where the macvlan will be put.");
        fprintf(stderr, "- mac: mac address of the macvlan in lower case (optional; random if not provied).");
        return -1;
    }

    // Do some rudimentary parsing of the command line arguments
    char ifname[IFNAMSIZ];
    strncpy(ifname, argv[1], IFNAMSIZ);
    ifname[IFNAMSIZ-1] = 0;
    pid_t pid = atoi(argv[2]);

    // Initialize the netlink socket
    char buf[4096];
    int netfd = socket(AF_NETLINK, SOCK_DGRAM, NETLINK_ROUTE);
    if (netfd < 0) {
        perror("socket(AF_NETLINK)");
        return -errno;
    }

    struct sockaddr_nl sa;
    sa.nl_family = AF_NETLINK;
    sa.nl_groups = RTMGRP_LINK;
    if (bind(netfd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
        perror("bind");
        return -errno;
    }

    struct
    {
        struct nlmsghdr hdr;
        struct ifinfomsg ifinfo;
        char attrbuf[512];
    } req;

    // Send a request to obtain the link index of the provided link
    memset(&req, 0, sizeof(req));
    req.hdr.nlmsg_len = NLMSG_LENGTH(sizeof(req.ifinfo));
    req.hdr.nlmsg_flags = NLM_F_REQUEST;
    req.hdr.nlmsg_type = RTM_GETLINK;

    req.ifinfo.ifi_family = AF_UNSPEC;
    req.ifinfo.ifi_index = 0;
    req.ifinfo.ifi_change = 0xFFFFFFFF; 

    int n = 512;
    struct rtattr *rta0 = (struct rtattr *)(((char *)&req) + NLMSG_ALIGN(req.hdr.nlmsg_len));
    rta0->rta_type = IFLA_IFNAME;
    rta0->rta_len = RTA_LENGTH(strlen(ifname));
    strcpy(RTA_DATA(rta0), ifname);
    rta0 = RTA_NEXT(rta0, n);

    req.hdr.nlmsg_len = NLMSG_ALIGN(req.hdr.nlmsg_len) + (512 - n);

    sendnl(netfd, &req.hdr);

    n = read(netfd, buf, 4096);

    int ifindex = 0;
    for (struct nlmsghdr *hdr = (struct nlmsghdr *)buf; NLMSG_OK(hdr, n); hdr = NLMSG_NEXT(hdr, n)) {
        if (hdr->nlmsg_type == NLMSG_DONE) {
            break;
        }

        if (hdr->nlmsg_type == NLMSG_ERROR) {
            struct nlmsgerr *err = (struct nlmsgerr *)NLMSG_DATA(hdr);
            if (err->error < 0) {
                fprintf(stderr, "rtnetlink (%d): %s\n", err->error, strerror(-err->error));
                return -err->error;
            }
        }

        if (hdr->nlmsg_type == RTM_NEWLINK) {
            memcpy(&req, hdr, sizeof(struct nlmsghdr) + sizeof(struct ifinfomsg));
            ifindex = req.ifinfo.ifi_index;
        }
    }

    if (!ifindex) {
        fprintf(stderr, "Could not locate interface.\n");
        return -1;
    }

    // Create the macvlan
    memset(&req, 0, sizeof(req));
    req.hdr.nlmsg_len = NLMSG_LENGTH(sizeof(req.ifinfo));
    req.hdr.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE;
    req.hdr.nlmsg_type = RTM_NEWLINK;

    req.ifinfo.ifi_family = AF_UNSPEC;
    req.ifinfo.ifi_index = 0;
    req.ifinfo.ifi_change = 0xFFFFFFFF; 

    n = 512;

    struct rtattr *rta = (struct rtattr *)(((char *)&req) + NLMSG_ALIGN(req.hdr.nlmsg_len));

    rta->rta_type = IFLA_LINK;
    rta->rta_len = RTA_LENGTH(sizeof(ifindex));
    memcpy(RTA_DATA(rta), &ifindex, sizeof(ifindex));
    rta = RTA_NEXT(rta, n);

    const char *macvlan = "macvlan0";
    rta->rta_type = IFLA_IFNAME;
    rta->rta_len = RTA_LENGTH(strlen(macvlan));
    strcpy(RTA_DATA(rta), macvlan);
    rta = RTA_NEXT(rta, n);

    rta->rta_type = IFLA_NET_NS_PID;
    rta->rta_len = RTA_LENGTH(sizeof(pid));
    memcpy(RTA_DATA(rta), &pid, sizeof(pid));
    rta = RTA_NEXT(rta, n);

    if (argc > 3) {
        unsigned char mac[6] = { 0 };
        if (argc > 3) {
            sscanf(argv[3], "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx",
                &mac[0], &mac[1], &mac[2], &mac[3], &mac[4], &mac[5]);
        }
        rta->rta_type = IFLA_ADDRESS;
        rta->rta_len = RTA_LENGTH(sizeof(mac));
        memcpy(RTA_DATA(rta), mac, sizeof(mac));
        rta = RTA_NEXT(rta, n);
    }

    rta->rta_type = IFLA_LINKINFO;
    struct rtattr *subrta = RTA_DATA(rta);
    subrta->rta_type = IFLA_INFO_KIND;
    subrta->rta_len = RTA_LENGTH(strlen("macvlan"));
    strcpy(RTA_DATA(subrta), "macvlan");
    rta->rta_len = RTA_ALIGN(RTA_LENGTH(subrta->rta_len));
    rta = RTA_NEXT(rta, n);

    req.hdr.nlmsg_len = NLMSG_ALIGN(req.hdr.nlmsg_len) + (512 - n);

    sendnl(netfd, &req.hdr);

    n = read(netfd, buf, 4096);

    for (struct nlmsghdr *hdr = (struct nlmsghdr *)buf; NLMSG_OK(hdr, n); hdr = NLMSG_NEXT(hdr, n)) {
        if (hdr->nlmsg_type == NLMSG_DONE) {
            break;
        }

        if (hdr->nlmsg_type == NLMSG_ERROR) {
            struct nlmsgerr *err = (struct nlmsgerr *)NLMSG_DATA(hdr);
            if (err->error < 0) {
                fprintf(stderr, "rtnetlink (%d): %s\n", err->error, strerror(-err->error));
                return -err->error;
            }
        }
    }

    return 0;
}